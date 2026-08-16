"""OpenAI-compatible endpoints for 清小搭 marketplace integration.

Implements the contract from openai-compatible-agent-integration-guide.md:
  - GET  /v1/models           — connectivity + credential check
  - POST /v1/chat/completions — real streaming SSE + non-streaming JSON

Beyond L0:
  - true streaming: frames are forwarded as the agent runs (thinking ->
    delta.reasoning, tool progress -> reasoning, answer -> delta.content),
    never the old run-to-completion-then-replay behavior;
  - multi-turn: conversation-keyed in-process session memory (TTL 2h, no
    cross-conversation memory); on a miss the session is seeded from the
    request's own message array;
  - multimodal input: content arrays with text / file (downloaded, parsed,
    RAG-indexed) / image_url / input_audio (reserved MediaAdapter, honest
    degradation on the current text-only model);
  - x_soda.attachments: research reports generated this turn are attached
    as downloadable markdown files (stop frame / response top-level);
  - max_tokens: accepted; soft cap on completion chars -> finish_reason
    "length"; finish_reason only ever uses the 5-value whitelist;
  - streaming errors after validation: in-band stop/error frame + [DONE];
    non-streaming upstream failures return a JSON 5xx response.
"""
from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

router = APIRouter(prefix="/v1", tags=["openai-compat"])

_TOOL_PROGRESS_TEXT = {
    "search_papers": "正在检索论文…",
    "deep_read": "正在深读论文…",
    "ask_papers": "正在查阅会话知识库…",
    "research_map": "正在生成研究地图…",
    "reading_path": "正在规划阅读路径…",
    "write_review": "正在撰写文献综述…",
    "citation_export": "正在导出参考文献…",
    "export_report": "正在导出研究报告…",
    "check_structure": "正在体检论文结构…",
    "check_format": "正在检查论文格式…",
    "export_manuscript": "正在导出文稿文件…",
}

# Tools whose success produces a downloadable report artifact.
_ARTIFACT_TOOLS = {"research_map", "write_review"}

_HEARTBEAT_SECONDS = 15.0


def _check_auth(authorization: str | None):
    """Validate a Bearer credential and return a non-secret API principal.

    The principal is stable for a database Agent key (its key id) and for the
    emergency/development fallback (a one-way token digest).  Raw credentials
    are never attached to the session, Checkpoint, trace, response or log.
    """
    from core.api_checkpoint import ApiCredentialPrincipal, principal_from_token

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header. Expected: Bearer <token>",
        )
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty credential")

    expected = os.getenv("AGENT_API_KEY", "").strip()
    if expected and hmac.compare_digest(token, expected):
        return principal_from_token(token, source="emergency_env")

    database_principal = None
    try:
        from core.user_store import resolve_agent_api_key
        database_principal = resolve_agent_api_key(token)
    except Exception:  # DB availability must not accidentally open production.
        database_principal = None
    if database_principal:
        return ApiCredentialPrincipal(
            credential_id=str(database_principal["key_id"]),
            key_id=str(database_principal["key_id"]),
            created_by=str(database_principal.get("created_by") or ""),
            source="database",
        )

    from app.core.config import settings
    if settings.app_env.lower() == "production" and not expected:
        try:
            from core.user_store import count_agent_api_keys
            no_keys = count_agent_api_keys() == 0
        except Exception:
            no_keys = True
        if no_keys:
            raise HTTPException(status_code=503, detail="Agent API credential is not configured")
    if settings.app_env.lower() != "production" and not expected:
        # Local development retains the documented non-empty-token convenience,
        # but each arbitrary token remains isolated by its one-way principal.
        return principal_from_token(token, source="development")
    raise HTTPException(status_code=401, detail="Invalid credential")


@router.get("/models")
async def list_models(authorization: str | None = Header(None)):
    """GET /v1/models — connectivity + credential check."""
    _check_auth(authorization)
    return {
        "object": "list",
        "data": [
            {"id": "paper-agent", "object": "model", "owned_by": "paper-agent"},
        ],
    }



_ALLOWED_ROLES = {"system", "user", "assistant"}
_ALLOWED_PART_TYPES = {"text", "image_url", "input_audio", "file"}


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    role: str
    content: str | list[dict]

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in _ALLOWED_ROLES:
            raise ValueError("role must be system, user, or assistant")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | list[dict]) -> str | list[dict]:
        if isinstance(value, str):
            return value
        for part in value:
            if not isinstance(part, dict):
                raise ValueError("content array entries must be objects")
            ptype = part.get("type")
            if ptype not in _ALLOWED_PART_TYPES:
                raise ValueError(f"unsupported content part type: {ptype!r}")
            if ptype == "text" and not isinstance(part.get("text"), str):
                raise ValueError("text part requires a string text field")
            if ptype == "image_url":
                image = part.get("image_url")
                if not isinstance(image, dict) or not isinstance(image.get("url"), str) or not image["url"].strip():
                    raise ValueError("image_url part requires image_url.url")
            if ptype == "input_audio":
                audio = part.get("input_audio")
                if not isinstance(audio, dict) or not isinstance(audio.get("url"), str) or not audio["url"].strip():
                    raise ValueError("input_audio part requires input_audio.url")
            if ptype == "file":
                file_data = part.get("file")
                if not isinstance(file_data, dict):
                    raise ValueError("file part requires a file object")
                url = file_data.get("url")
                file_id = file_data.get("file_id")
                if not ((isinstance(url, str) and url.strip()) or
                        (isinstance(file_id, str) and file_id.strip())):
                    raise ValueError("file part requires file.url or file.file_id")
        return value


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    messages: list[ChatMessageRequest] = Field(min_length=1)
    stream: StrictBool = False
    model: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    user: str | None = Field(default=None, max_length=128)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str | None) -> str | None:
        return value

    @field_validator("messages")
    @classmethod
    def require_user_message(cls, value: list[ChatMessageRequest]) -> list[ChatMessageRequest]:
        if not any(message.role == "user" for message in value):
            raise ValueError("messages must contain at least one user message")
        return value

# ---------------------------------------------------------------------------
# Request preparation
# ---------------------------------------------------------------------------

async def _prepare_turn(body: dict, principal):
    """Resolve a user turn against the API-only persistent Checkpoint store."""
    from app.api.v1.multimodal import extract_user_content, process_media_parts
    from core.api_checkpoint import get_api_checkpoint_store
    from core.session_memory import seed_session_from_messages

    messages_in = body.get("messages") or []
    last_user = next((m for m in reversed(messages_in) if m.get("role") == "user"), None)
    text, media = extract_user_content(last_user or {})
    openai_user = str(body.get("user") or "").strip()[:128]

    checkpoint_store = get_api_checkpoint_store()
    loaded = checkpoint_store.get_or_create(principal, openai_user, messages_in)
    session = loaded.session
    if loaded.created:
        # The request itself is the only temporary source for text history on a
        # first/missing checkpoint. It is deliberately not written to state.db.
        seed_session_from_messages(session, messages_in)

    if media and session.storage_context is not None:
        from core.storage_pressure import StoragePressureError, StoragePressureGuard
        guard = StoragePressureGuard(session.storage_context)
        try:
            async with guard.protect("upload", session.session_id):
                notes, attachments, errors = await process_media_parts(
                    media, storage_context=session.storage_context, session_id=session.session_id
                )
        except StoragePressureError as exc:
            notes, attachments = [], []
            errors = [
                f"存储空间达到 {exc.threshold}% 保护阈值，当前暂停上传/下载；普通文字问答仍可继续。"
            ]
    else:
        notes, attachments, errors = await process_media_parts(
            media, storage_context=session.storage_context, session_id=session.session_id
        )
    if errors:
        notes.extend(f"[{e}]" for e in errors)

    user_message = text or ("请分析我上传的内容。" if (media or notes) else "(empty)")
    if notes:
        user_message += "\n" + "\n".join(notes)

    return user_message, session, attachments, checkpoint_store, messages_in, openai_user


def _chat_turn_stream(chat_turn, user_message, session, attachments, checkpoint_cb):
    """Keep test/third-party legacy ``chat_turn`` callables compatible."""
    kwargs = {"attachments": attachments or None}
    try:
        signature = inspect.signature(chat_turn)
        if "checkpoint_cb" in signature.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
        ):
            kwargs["checkpoint_cb"] = checkpoint_cb
    except (TypeError, ValueError):
        # The real orchestrator has an inspectable Python signature.  If a
        # wrapper does not, use its compatibility-safe historical call shape.
        pass
    return chat_turn(user_message, session, None, **kwargs)


def _base_file_url(request: Request) -> str:
    """Deterministic public origin for attachment URLs.

    Production deployments should set PUBLIC_BASE_URL. ``request.base_url`` is
    only a development fallback and is never reconstructed from arbitrary
    forwarded headers here.
    """
    from app.core.config import settings
    configured = (settings.public_base_url or "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


_FILE_TYPES = {"image", "audio", "video", "pdf", "word", "excel",
               "ppt", "text", "archive", "file"}


def _infer_file_type(mime_type: str, filename: str = "") -> str:
    mime = (mime_type or "").split(";", 1)[0].lower()
    suffix = Path(filename).suffix.lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if "word" in mime or suffix in {".doc", ".docx"}:
        return "word"
    if "excel" in mime or "spreadsheet" in mime or suffix in {".xls", ".xlsx"}:
        return "excel"
    if "powerpoint" in mime or "presentation" in mime or suffix in {".ppt", ".pptx"}:
        return "ppt"
    if mime.startswith("text/") or suffix in {".txt", ".md", ".tex", ".bib"}:
        return "text"
    if mime in {"application/zip", "application/x-rar-compressed",
                "application/x-7z-compressed"}:
        return "archive"
    return "file"


def _public_file_url(request: Request, filename: str, relative_url: str = "") -> str:
    base = _base_file_url(request) + "/"
    if relative_url:
        parsed = urlsplit(relative_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return relative_url
        path = parsed.path
    else:
        path = f"/files/{filename}"
    encoded = "/".join(quote(segment, safe="") for segment in path.split("/") if segment)
    return urljoin(base, encoded)


def _shape_attachment(request: Request, record: dict) -> dict | None:
    public_name = str(record.get("fileName") or "").strip()
    filename = str(record.get("displayName") or public_name).strip()
    if not public_name or not filename:
        return None
    mime_type = str(record.get("mimeType") or "application/octet-stream").strip()
    raw_type = str(record.get("fileType") or "").strip()
    file_type = raw_type if raw_type in _FILE_TYPES else _infer_file_type(mime_type, filename)
    try:
        size = max(0, int(record.get("size") or record.get("fileSize") or 0))
    except (TypeError, ValueError):
        size = 0
    return {
        "fileUrl": _public_file_url(request, public_name, str(record.get("url") or "")),
        "fileName": filename,
        "fileType": file_type,
        "mimeType": mime_type,
        "fileSize": size,
    }


def _dedupe_attachments(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("fileUrl") or ""), str(item.get("fileName") or ""))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _usage_of(done_event: dict | None, est_prompt: int = 0, est_completion: int = 0) -> dict:
    """Return real usage when available, otherwise a documented estimate."""
    if done_event and isinstance(done_event.get("usage"), dict):
        u = done_event["usage"]
        total = int(u.get("total_tokens") or 0)
        if total > 0:
            return {
                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                "completion_tokens": int(u.get("completion_tokens") or 0),
                "total_tokens": total,
            }
    p, c = max(0, est_prompt), max(0, est_completion)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _reports_for(request: Request, session, kinds: set[str]) -> list[dict]:
    if not kinds:
        return []
    from tools.export.report import write_reports
    return [att for rec in write_reports(session, kinds)
            if (att := _shape_attachment(request, rec)) is not None]


def _tool_result_files(request: Request, result: dict) -> list[dict]:
    return [att for rec in ((result.get("data") or {}).get("files") or [])
            if isinstance(rec, dict)
            if (att := _shape_attachment(request, rec)) is not None]


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------

@router.post("/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(None)):
    principal = _check_auth(authorization)
    try:
        raw_body = await request.json()
    except Exception as exc:  # Starlette intentionally leaves JSON parsing to us.
        raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc
    if not isinstance(raw_body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    try:
        parsed = ChatCompletionRequest.model_validate(raw_body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False)) from exc
    body = parsed.model_dump(exclude_none=False)
    stream = parsed.stream
    max_tokens = parsed.max_tokens

    from agents.orchestrator import chat_turn

    user_message, session, attachments, checkpoint_store, request_messages, openai_user = await _prepare_turn(body, principal)
    if session.storage_context is not None:
        from core.storage_pressure import StoragePressureGuard
        StoragePressureGuard(session.storage_context).ensure_allowed("text_chat")

    async def checkpoint_cb(active_session, *, final=False, final_answer=None):
        await checkpoint_store.checkpoint_callback(
            active_session, final=final, final_answer=final_answer
        )

    async def finalize_checkpoint(final_answer: str) -> None:
        await checkpoint_store.finalize_turn(
            session,
            principal=principal,
            openai_user=openai_user,
            messages=request_messages,
            final_answer=final_answer,
        )

    cid = f"chatcmpl-{int(time.time() * 1000)}"
    created = int(time.time())
    # ~4 chars per token as a soft cap on completion content.
    content_budget = max_tokens * 4 if max_tokens else None
    # chars/4 token estimate (provider returns no stream usage).
    est_prompt = sum(
        len(str(m.get("content", ""))) for m in (body.get("messages") or [])
    ) // 4

    if not stream:
        # ---- non-streaming: run to completion, return one JSON ----
        thinking_parts: list[str] = []
        answer_parts: list[str] = []
        artifact_kinds: set[str] = set()
        result_files: list[dict] = []
        done_event: dict | None = None
        error_msg: str | None = None
        truncated = False

        async for ev in _chat_turn_stream(
            chat_turn, user_message, session, attachments, checkpoint_cb
        ):
            etype = ev.get("type")
            if etype == "thinking" and ev.get("is_delta"):
                thinking_parts.append(ev.get("content", ""))
            elif etype == "answer" and ev.get("is_delta"):
                answer_parts.append(ev.get("content", ""))
            elif etype == "tool_result":
                tool = (ev.get("result") or {}).get("tool", "")
                status = (ev.get("result") or {}).get("status", "")
                if tool in _ARTIFACT_TOOLS and status == "success":
                    artifact_kinds.add(tool)
                elif status == "success":
                    result_files.extend(
                        _tool_result_files(request, ev.get("result") or {}))
            elif etype == "done":
                done_event = ev
            elif etype == "error":
                error_msg = ev.get("message", "unknown error")

        answer = "".join(answer_parts).strip()
        thinking = "".join(thinking_parts).strip()
        if not answer and error_msg:
            return JSONResponse(
                status_code=500,
                content={"error": {"type": "upstream_error", "message": error_msg}},
            )
        if not answer:
            answer = thinking or "(no response)"
        if content_budget is not None and len(answer) > content_budget:
            answer = answer[:content_budget]
            truncated = True
        if done_event is not None and error_msg is None:
            await finalize_checkpoint(answer)

        resp = {
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "length" if truncated else "stop",
            }],
            "usage": _usage_of(done_event, est_prompt,
                               (len(answer) + len(thinking)) // 4),
        }
        attachments_out = _dedupe_attachments(
            _reports_for(request, session, artifact_kinds) + result_files)
        if attachments_out:
            resp["x_soda"] = {"attachments": attachments_out}
        return JSONResponse(resp)

    # ---- streaming: open immediately; all post-validation failures use an
    # in-band stop/error frame followed by [DONE]. ----
    queue: asyncio.Queue = asyncio.Queue()

    async def produce():
        try:
            async for ev in _chat_turn_stream(
                chat_turn, user_message, session, attachments, checkpoint_cb
            ):
                await queue.put(ev)
        except Exception as exc:  # noqa: BLE001
            await queue.put({"type": "error", "message": str(exc)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(produce())

    async def frame_stream():
        def frame(delta: dict, finish: str | None = None,
                  usage: dict | None = None, x_soda: dict | None = None,
                  error: dict | None = None) -> str:
            choice = {"index": 0, "delta": delta, "finish_reason": finish}
            chunk = {"id": cid, "object": "chat.completion.chunk",
                     "created": created, "choices": [choice]}
            if usage:
                chunk["usage"] = usage
            if x_soda:
                chunk["x_soda"] = x_soda
            if error:
                chunk["error"] = error
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        yield frame({"role": "assistant"})  # role frame, exactly once, first

        artifact_kinds: set[str] = set()
        result_files: list[dict] = []
        done_event: dict | None = None
        content_sent = 0
        content_parts: list[str] = []
        truncated = False
        finished = False
        terminal_emitted = False  # an error frame already IS the stop frame

        def handle(ev: dict) -> str | None:
            """Map one agent event to an SSE frame (None = no frame)."""
            nonlocal content_sent, truncated, done_event, finished, terminal_emitted
            etype = ev.get("type")
            if etype == "thinking" and ev.get("is_delta"):
                return frame({"reasoning": ev.get("content", "")})
            if etype == "answer" and ev.get("is_delta"):
                if truncated:
                    return None
                piece = ev.get("content", "")
                if content_budget is not None and content_sent + len(piece) > content_budget:
                    piece = piece[:max(0, content_budget - content_sent)]
                    truncated = True
                if piece:
                    content_sent += len(piece)
                    content_parts.append(piece)
                    return frame({"content": piece})
                return None
            if etype == "tool_start":
                name = ev.get("name", "")
                if name == "use_skill":
                    return None
                return frame({"reasoning": _TOOL_PROGRESS_TEXT.get(name, "正在继续处理当前任务…")})
            if etype == "tool_warning":
                return frame({"reasoning": f"注意：{ev.get('warning', '')}"})
            if etype == "tool_result":
                result = ev.get("result") or {}
                if result.get("tool") == "use_skill":
                    return None
                if result.get("tool") in _ARTIFACT_TOOLS and result.get("status") == "success":
                    artifact_kinds.add(result["tool"])
                elif result.get("status") == "success":
                    result_files.extend(_tool_result_files(request, result))
                return None
            if etype == "done":
                done_event = ev
                finished = True
                return None
            if etype == "error":
                finished = True
                terminal_emitted = True
                return frame({}, finish="stop",
                             usage=_usage_of(done_event, est_prompt,
                                             content_sent // 4),
                             error={"type": "upstream_error",
                                    "message": ev.get("message", "unknown error")})
            return None

        # Replay buffered events, then consume the queue live (with heartbeats
        # during long tool runs so the gateway never sees a dead stream).
        streams: list[dict] = []
        while True:
            if streams:
                ev = streams.pop(0)
            else:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield frame({"reasoning": "仍在处理中，请稍候…"})
                    continue
                if ev is None:
                    break
            out = handle(ev)
            if out is not None:
                yield out
            if finished:
                break

        attachments_out = _dedupe_attachments(
            _reports_for(request, session, artifact_kinds) + result_files)
        if not terminal_emitted and done_event is not None:
            await finalize_checkpoint("".join(content_parts))
        if not terminal_emitted:
            x_soda = {"attachments": attachments_out} if attachments_out else None
            yield frame({}, finish="length" if truncated else "stop",
                        usage=_usage_of(done_event, est_prompt,
                                        content_sent // 4), x_soda=x_soda)
        yield "data: [DONE]\n\n"

        if not task.done():
            task.cancel()

    return StreamingResponse(
        frame_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
