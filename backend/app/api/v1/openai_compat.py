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
  - x_soda.attachments: research-map SVG/HTML/Markdown and other artifacts
    generated this turn are attached (stop frame / response top-level); Mermaid
    remains ordinary assistant content rather than an attachment;
  - markdown card emulation: tool results render as compact markdown cards
    (tools/export/cards.py) inserted into delta.content when a tool
    completes, before the final answer — mirroring the self-hosted frontend
    layout. 清小搭's guide defines no structured-content/HTML/WebView
    channel, so content + reasoning + attachments are the only surfaces.
    The admin-configurable display policy (api_display_policy) picks which
    tools get full cards; skill loading surfaces through the dedicated
    skill_loaded event as a thinking-fold line plus an in-content line;
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
import logging
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
logger = logging.getLogger(__name__)

_TOOL_PROGRESS_TEXT = {
    "search_papers": "🔎 正在检索文献…",
    "deep_read": "📖 正在深读论文…",
    "ask_papers": "💬 正在查阅会话知识库…",
    "research_map": "🗺️ 正在构建研究地图…",
    "reading_path": "🧭 正在规划阅读路径…",
    "write_review": "📝 正在撰写文献综述…",
    "citation_export": "📑 正在导出参考文献…",
    "export_report": "📥 正在导出研究报告…",
    "check_structure": "📋 正在体检论文结构…",
    "check_format": "🧾 正在检查论文格式…",
    "export_manuscript": "📄 正在导出文稿文件…",
    "integrity_sweep": "🛡️ 正在做可靠性质检…",
    "bib_import": "📚 正在导入文献库…",
    "exhibit_index": "🖼️ 正在提取图表导览…",
    "explain_element": "🔍 正在解读图表/公式…",
    "field_census": "📊 正在做领域普查…",
}


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _tool_progress_text(name: str, args: dict) -> str:
    """Args-aware progress line for the thinking fold."""
    if name == "search_papers" and args.get("topic"):
        return f"🔎 正在检索：{_clip(args['topic'], 40)}…"
    if name == "deep_read" and args.get("attachment_ids"):
        return f"📖 正在深读 {len(args['attachment_ids'])} 个上传文件…"
    if name == "ask_papers" and args.get("query"):
        return f"💬 正在查阅文献：{_clip(args['query'], 40)}…"
    if name == "explain_element" and args.get("element_id"):
        return f"🔍 正在解读元素：{_clip(args['element_id'], 44)}…"
    if name == "write_review":
        return "📝 正在撰写文献综述…"
    return _TOOL_PROGRESS_TEXT.get(name, "⏳ 正在继续处理当前任务…")

# Tools whose success produces a downloadable report artifact.
_ARTIFACT_TOOLS = {"research_map", "write_review"}

_HEARTBEAT_SECONDS = 10.0
# Test/embedding override hooks only; production deadlines come from the
# administrator-adjustable tool_budget_policy snapshot (defaults 95/105).
_API_SOFT_DEADLINE_SECONDS = 95.0
_API_HARD_DEADLINE_SECONDS = 105.0
_SSE_COALESCE_SECONDS = 0.05
_SSE_COALESCE_CHARS = 256


def _load_agent_stack():
    """Import the heavy chat stack and return the callables used by this route."""
    from agents.orchestrator import chat_turn
    from tools.export.cards import (
        render_search_table, render_skill_card, render_tool_card, skill_display_title,
    )
    return chat_turn, render_skill_card, render_tool_card, render_search_table, skill_display_title


async def _load_agent_stack_async(*, threaded: bool):
    if threaded:
        return await asyncio.to_thread(_load_agent_stack)
    return _load_agent_stack()


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



_ALLOWED_ROLES = {"system", "user", "assistant", "tool"}
_ALLOWED_PART_TYPES = {"text", "image_url", "input_audio", "file"}


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    role: str
    content: str | list[dict]

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in _ALLOWED_ROLES:
            raise ValueError("role must be system, user, assistant, or tool")
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
                filename = file_data.get("filename")
                if url is not None and not isinstance(url, str):
                    raise ValueError("file.url must be a string when provided")
                if file_id is not None and not isinstance(file_id, str):
                    raise ValueError("file.file_id must be a string when provided")
                if filename is not None and not isinstance(filename, str):
                    raise ValueError("file.filename must be a string when provided")
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
    sessionId: str | None = Field(default=None, max_length=128)

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
    session_id = str(body.get("sessionId") or "").strip()[:128]
    checkpoint_scope = openai_user
    caller_system = "\n\n".join(
        str(m.get("content") or "").strip()
        for m in messages_in
        if m.get("role") == "system" and isinstance(m.get("content"), str)
        and str(m.get("content") or "").strip()
    )[:8000]

    checkpoint_store = get_api_checkpoint_store()
    loaded = checkpoint_store.get_or_create(
        principal, checkpoint_scope, messages_in, provider_session_id=session_id or None
    )
    session = loaded.session
    # Full visible messages are request-scoped text context on every turn.  They
    # replace any transient in-process copy but are never written to Checkpoint.
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

    return (user_message, session, attachments, checkpoint_store,
            messages_in, checkpoint_scope, caller_system)


def _chat_turn_stream(chat_turn, user_message, session, attachments, checkpoint_cb,
                      *, progress_cb=None, execution_context=None,
                      system_instructions: str = ""):
    """Keep test/third-party legacy ``chat_turn`` callables compatible."""
    kwargs = {"attachments": attachments or None}
    try:
        signature = inspect.signature(chat_turn)
        if "checkpoint_cb" in signature.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
        ):
            kwargs["checkpoint_cb"] = checkpoint_cb
        if "execution_context" in signature.parameters:
            kwargs["execution_context"] = execution_context
        if "system_instructions" in signature.parameters:
            kwargs["system_instructions"] = system_instructions
    except (TypeError, ValueError):
        # The real orchestrator has an inspectable Python signature.  If a
        # wrapper does not, use its compatibility-safe historical call shape.
        pass
    return chat_turn(user_message, session, progress_cb, **kwargs)


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
    attachment = {
        "fileUrl": _public_file_url(request, public_name, str(record.get("url") or "")),
        "fileName": filename,
        "fileType": file_type,
        "mimeType": mime_type,
        "fileSize": size,
    }
    if file_type == "image":
        # Optional per the attachments spec; gives hosts a thumbnail URL.
        attachment["previewUrl"] = attachment["fileUrl"]
    return attachment


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


def _reports_for(
    request: Request, session, kinds: set[str], *,
    research_map_svg_enabled: bool = True,
    research_map_html_enabled: bool = False,
    research_map_markdown_enabled: bool = False,
) -> list[dict]:
    if not kinds:
        return []
    from tools.export.report import write_reports
    return [
        att for rec in write_reports(
            session, kinds,
            research_map_svg_enabled=research_map_svg_enabled,
            research_map_html_enabled=research_map_html_enabled,
            research_map_markdown_enabled=research_map_markdown_enabled,
        )
        if (att := _shape_attachment(request, rec)) is not None
    ]


def _tool_result_files(request: Request, result: dict) -> list[dict]:
    return [att for rec in ((result.get("data") or {}).get("files") or [])
            if isinstance(rec, dict)
            if (att := _shape_attachment(request, rec)) is not None]


def _load_display_policy(session):
    """Admin display policy for this turn; defaults on any storage failure —
    a chat turn must never fail because of a display-policy read."""
    from core.api_storage_store import ApiDisplayPolicy
    try:
        if session.storage_context is not None:
            from core.api_storage_store import ApiStorageStore
            return ApiStorageStore(session.storage_context).get_display_policy()
    except Exception:  # noqa: BLE001
        pass
    return ApiDisplayPolicy()


def _save_api_export(session, text: str | None, *, data: bytes | None,
                     display_name: str, mime_type: str, file_type: str) -> dict | None:
    """Persist one API-channel export blob and return its file record."""
    if session.storage_context is None or session.channel != "openai_api":
        return None
    import uuid
    from core.api_artifact_store import ApiArtifactStore
    from core.api_storage_store import ApiStorageStore

    context = session.storage_context
    context.ensure_layout()
    temp = context.temp_dir / f"card-{uuid.uuid4().hex}.tmp"
    try:
        if data is not None:
            temp.write_bytes(data)
        elif text:
            temp.write_text(text, encoding="utf-8")
        else:
            return None
        artifact = ApiArtifactStore(ApiStorageStore(context)).save_export(
            temp, session_id=session.session_id,
            display_name=display_name, mime_type=mime_type,
        )
        return {
            "fileName": artifact.public_alias,
            "displayName": display_name,
            "fileType": file_type,
            "mimeType": mime_type,
            "path": str(artifact.path),
            "size": artifact.size_bytes,
            "url": f"/files/{artifact.public_alias}",
        }
    except Exception:  # noqa: BLE001 — attachments are best-effort per file
        return None
    finally:
        temp.unlink(missing_ok=True)


def _extra_attachments(request: Request, session, tool_results: list[dict], *,
                       bibtex_export_mode: str = "md_only") -> list[dict]:
    """Best-effort extra x_soda file cards: element crop PNG, citation
    export file, field-census trend SVG. Reads only this turn's tool
    results; the explain_element ownership guard is re-checked here.
    bibtex_export_mode (admin display policy) decides whether a BibTeX
    citation export rides a .bib attachment, a byte-identical .md copy
    (清小搭 cannot download .bib), or both."""
    out: list[dict] = []
    seen_names: set[str] = set()

    def add(record: dict | None) -> None:
        if record is None:
            return
        name = record.get("displayName") or record.get("fileName")
        if not name or name in seen_names:
            return
        seen_names.add(name)
        attachment = _shape_attachment(request, record)
        if attachment is not None:
            out.append(attachment)

    import time
    import uuid as _uuid
    from pathlib import Path as _Path

    from tools.export.cards import render_field_census_svg

    for result in tool_results:
        tool = result.get("tool", "")
        if result.get("status") == "error":
            continue
        if tool == "explain_element":
            element = result.get("element")
            if not isinstance(element, dict) or element.get("kind") not in {"figure", "table"}:
                continue
            paper_id = str(element.get("paper_id") or "")
            asset_url = str(element.get("asset_url") or "")
            fname = _Path(asset_url).name if asset_url else ""
            if not paper_id or not fname:
                continue
            try:
                from tools.ingest.attachments import session_element_scope
                if paper_id not in set(session_element_scope(session)):
                    continue
            except Exception:  # noqa: BLE001 — scope check must not crash exports
                continue
            from core.config import get_settings
            from tools.pdf.fetcher import _sanitize_filename_component as _sanitize
            crop = (_Path(get_settings().reader.assets_dir)
                    / _sanitize(paper_id) / fname)
            if not crop.is_file():
                continue
            add(_save_api_export(
                session, None, data=crop.read_bytes(),
                display_name=f"element_{_sanitize(paper_id)[:40]}_{fname}",
                mime_type="image/png", file_type="image"))
        elif tool == "citation_export":
            citations = str(result.get("citations") or "").strip()
            if not citations:
                continue
            stamp = time.strftime("%Y%m%d_%H%M%S")
            if result.get("format") == "gbt7714":
                add(_save_api_export(
                    session, citations, data=None,
                    display_name=f"references_GB_T7714_{stamp}_{_uuid.uuid4().hex[:4]}.txt",
                    mime_type="text/plain", file_type="text"))
                continue
            # BibTeX: 清小搭 cannot download .bib files, so the admin policy
            # decides between the legacy .bib attachment, a byte-identical
            # .md copy (rename back to .bib), or both.
            if bibtex_export_mode != "md_only":
                add(_save_api_export(
                    session, citations, data=None,
                    display_name=f"references_bibtex_{stamp}_{_uuid.uuid4().hex[:4]}.bib",
                    mime_type="text/plain", file_type="text"))
            if bibtex_export_mode != "bib_only":
                add(_save_api_export(
                    session, citations, data=None,
                    display_name=f"references_bibtex_{stamp}_{_uuid.uuid4().hex[:4]}.md",
                    mime_type="text/markdown", file_type="text"))
        elif tool == "field_census":
            svg = render_field_census_svg(result, title=str(session.topic or "领域普查"))
            if not svg:
                continue
            stamp = time.strftime("%Y%m%d_%H%M%S")
            add(_save_api_export(
                session, svg, data=None,
                display_name=f"field_census_{stamp}_{_uuid.uuid4().hex[:4]}.svg",
                mime_type="image/svg+xml", file_type="image"))
    return out


def _bibtex_md_note_needed(tool_results: list[dict], policy) -> bool:
    """True when a BibTeX export rode a .md attachment this turn, so the
    trailing rename notice must be appended to the formal output. Mirrors
    the _extra_attachments citation_export gate and is deliberately not
    governed by tool_cards_enabled — it explains the attachment behavior,
    not a decorative card."""
    if getattr(policy, "bibtex_export_mode", "bib_only") == "bib_only":
        return False
    return any(
        result.get("tool") == "citation_export"
        and result.get("status") != "error"
        and result.get("format") != "gbt7714"
        and str(result.get("citations") or "").strip()
        for result in tool_results
    )


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------

@router.post("/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(None)):
    request_started = time.monotonic()
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
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False),
        ) from exc

    body = parsed.model_dump(exclude_none=False)
    stream = parsed.stream
    max_tokens = parsed.max_tokens
    cid = f"chatcmpl-{int(time.time() * 1000)}"
    created = int(time.time())
    content_budget = max_tokens * 4 if max_tokens else None
    est_prompt = sum(
        len(str(m.get("content", ""))) for m in (body.get("messages") or [])
    ) // 4

    from core.runtime_performance_policy import get_performance_policy
    startup_mode = get_performance_policy().startup_prewarm_mode
    agent_stack = None
    from core.turn_execution import TurnExecutionContext
    from core.tool_budget_store import (
        CODE_FALLBACK_BUDGET, CODE_FALLBACK_RESERVE,
        API_TURN_SOFT_DEFAULT_SECONDS, API_TURN_HARD_DEFAULT_SECONDS,
        ToolBudgetPolicy, get_tool_budget_policy,
    )
    try:
        tool_budget_policy = get_tool_budget_policy()
    except Exception:
        logger.exception("tool_budget_policy_snapshot_failed")
        tool_budget_policy = ToolBudgetPolicy(
            budgets={}, default_budget_seconds=CODE_FALLBACK_BUDGET,
            reserve_seconds=CODE_FALLBACK_RESERVE,
            api_turn_soft_seconds=API_TURN_SOFT_DEFAULT_SECONDS,
            api_turn_hard_seconds=API_TURN_HARD_DEFAULT_SECONDS,
        )
    # Keep the historical module-level override usable by focused tests and
    # embedding callers; production uses the administrator snapshot.
    api_soft_seconds = (
        _API_SOFT_DEADLINE_SECONDS
        if _API_SOFT_DEADLINE_SECONDS != 95.0
        else tool_budget_policy.api_turn_soft_seconds
    )
    if _API_HARD_DEADLINE_SECONDS != 105.0:
        api_hard_seconds = _API_HARD_DEADLINE_SECONDS
    else:
        api_hard_seconds = tool_budget_policy.api_turn_hard_seconds
        # Defensive clamp for hand-edited databases: the soft deadline must
        # always fire before the hard cutoff so the degraded wrap-up answer
        # can still be emitted.
        api_hard_seconds = max(api_hard_seconds, api_soft_seconds + 5.0)

    async def prepare():
        prepared = await _prepare_turn(body, principal)
        (user_message, session, attachments, checkpoint_store,
         request_messages, checkpoint_scope, caller_system) = prepared
        if session.storage_context is not None:
            from core.storage_pressure import StoragePressureGuard
            StoragePressureGuard(session.storage_context).ensure_allowed("text_chat")
        policy = _load_display_policy(session)
        cards_allowed = policy.tool_cards_enabled
        # The mandated search-results table is part of the standardized formal
        # output, so it ignores the tool-card toggle and only bows to a tight
        # max_tokens budget where it would crowd out the answer itself.
        table_allowed = content_budget is None or content_budget >= 2000

        async def checkpoint_cb(active_session, *, final=False, final_answer=None):
            await checkpoint_store.checkpoint_callback(
                active_session, final=final, final_answer=final_answer
            )

        async def finalize_checkpoint(final_answer: str) -> None:
            await checkpoint_store.finalize_turn(
                session,
                principal=principal,
                openai_user=checkpoint_scope,
                messages=request_messages,
                final_answer=final_answer,
            )

        return {
            "user_message": user_message,
            "session": session,
            "attachments": attachments,
            "caller_system": caller_system,
            "policy": policy,
            "cards_allowed": cards_allowed,
            "table_allowed": table_allowed,
            "checkpoint_cb": checkpoint_cb,
            "finalize_checkpoint": finalize_checkpoint,
        }

    if not stream:
        agent_stack = _load_agent_stack()
        chat_turn, render_skill_card, render_tool_card, render_search_table, skill_display_title = agent_stack
        execution = TurnExecutionContext.openai_api(
            soft_timeout_seconds=api_soft_seconds,
            hard_timeout_seconds=api_hard_seconds,
            tool_budget_policy=tool_budget_policy,
        )
        try:
            async with asyncio.timeout(api_hard_seconds):
                prepared = await prepare()
                session = prepared["session"]
                policy = prepared["policy"]
                thinking_parts: list[str] = []
                answer_parts: list[str] = []
                block_parts: list[tuple[str, bool, str]] = []
                artifact_kinds: set[str] = set()
                result_files: list[dict] = []
                tool_results: list[dict] = []
                done_event: dict | None = None
                error_msg: str | None = None
                truncated = False

                async for ev in _chat_turn_stream(
                    chat_turn,
                    prepared["user_message"],
                    session,
                    prepared["attachments"],
                    prepared["checkpoint_cb"],
                    execution_context=execution,
                    system_instructions=prepared["caller_system"],
                ):
                    etype = ev.get("type")
                    if etype == "thinking" and ev.get("is_delta"):
                        thinking_parts.append(ev.get("content", ""))
                    elif etype == "answer" and ev.get("is_delta"):
                        answer_parts.append(ev.get("content", ""))
                    elif etype == "skill_loaded" and policy.skill_card_enabled:
                        block_parts.append((render_skill_card(
                            skill_display_title(str(ev.get("name") or ""))), False, "card"))
                    elif etype == "tool_result":
                        result = ev.get("result") or {}
                        tool = result.get("tool", "")
                        if tool == "use_skill":
                            continue
                        tool_results.append(result)
                        if result.get("status") == "success":
                            files = _tool_result_files(request, result)
                            if files:
                                result_files.extend(files)
                            elif tool in _ARTIFACT_TOOLS:
                                # Compatibility fallback for older tool results
                                # that did not create their own export records.
                                artifact_kinds.add(tool)
                        table = (render_search_table(result)
                                 if tool == "search_papers" and result.get("status") != "error"
                                 and prepared["table_allowed"] else None)
                        if table:
                            line = (render_tool_card(tool, result)
                                    if prepared["cards_allowed"] else "")
                            block_parts.append(
                                (f"{line}\n\n{table}" if line else table, True, "table"))
                        elif prepared["cards_allowed"] and (
                                result.get("status") != "error"
                                or policy.tool_error_cards_enabled):
                            # Error-status one-liners are model-steering text;
                            # the tool_error_cards_enabled policy (off by
                            # default) decides whether they reach the user.
                            card = render_tool_card(tool, result)
                            if card:
                                block_parts.append((card, False, "card"))
                        if tool == "research_map" and result.get("status") == "success" \
                                and policy.research_map_mermaid_enabled:
                            from tools.export.report import render_research_map_mermaid
                            mermaid = render_research_map_mermaid(session)
                            if mermaid:
                                block_parts.append((mermaid, False, "mermaid"))
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
                prefix = ""
                if block_parts:
                    card_share = content_budget * 0.6 if content_budget is not None else None
                    selected_blocks: list[str] = []
                    block_spent = 0
                    omitted = "研究图谱 Mermaid 正文因 max_tokens 预算不足已整块省略；附件仍按展示策略生成。"
                    for block, required, kind in block_parts:
                        candidate = block
                        if kind == "mermaid" and card_share is not None and \
                                block_spent + len(candidate) > card_share:
                            candidate, required = omitted, True
                        if not required and card_share is not None and \
                                block_spent + len(candidate) > card_share:
                            continue
                        selected_blocks.append(candidate)
                        block_spent += len(candidate)
                    if selected_blocks:
                        prefix = "\n\n".join(selected_blocks) + "\n\n"
                content = prefix + answer
                if _bibtex_md_note_needed(tool_results, policy):
                    # Must land before truncation and finalize_checkpoint so
                    # the response content and the checkpointed alias chain
                    # stay byte-identical.
                    from tools.export.cards import BIBTEX_MD_EXPORT_NOTE
                    content += "\n\n" + BIBTEX_MD_EXPORT_NOTE
                if content_budget is not None and len(content) > content_budget:
                    content = content[:content_budget]
                    truncated = True
                if done_event is not None and error_msg is None and \
                        not done_event.get("deadline_exceeded"):
                    await prepared["finalize_checkpoint"](content)

                resp = {
                    "id": cid,
                    "object": "chat.completion",
                    "created": created,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "length" if truncated else "stop",
                    }],
                    "usage": _usage_of(
                        done_event, est_prompt, (len(content) + len(thinking)) // 4
                    ),
                }
                attachments_out = _dedupe_attachments(
                    _reports_for(
                        request, session, artifact_kinds,
                        research_map_svg_enabled=policy.research_map_svg_enabled,
                        research_map_html_enabled=policy.research_map_html_enabled,
                        research_map_markdown_enabled=policy.research_map_markdown_enabled,
                    )
                    + result_files
                    + _extra_attachments(
                        request, session, tool_results,
                        bibtex_export_mode=policy.bibtex_export_mode)
                )
                if attachments_out:
                    resp["x_soda"] = {"attachments": attachments_out}
                return JSONResponse(resp)
        except TimeoutError:
            execution.cancel()
            return JSONResponse(
                status_code=504,
                content={"error": {
                    "type": "deadline_exceeded",
                    "message": (
                        "The agent turn exceeded the "
                        f"{api_hard_seconds:.0f} second interactive deadline."
                    ),
                }},
            )

    # blocking/background/off retain the normal load-before-response order.
    # role_first is the only mode that defers the import until after role.
    if startup_mode != "role_first":
        agent_stack = _load_agent_stack()

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    execution: TurnExecutionContext

    def progress_cb(message: str) -> None:
        event = {"type": "tool_progress", "message": str(message or "")[:500]}
        try:
            if asyncio.get_running_loop() is loop:
                queue.put_nowait(event)
            else:
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:
            # Called from a worker thread or after the request loop closed.
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass

    execution = TurnExecutionContext.openai_api(
        progress_cb=progress_cb,
        soft_timeout_seconds=api_soft_seconds,
        hard_timeout_seconds=api_hard_seconds,
        tool_budget_policy=tool_budget_policy,
    )

    async def frame_stream():
        frame_count = 0
        frame_bytes = 0
        last_frame_at: float | None = None
        max_idle_gap = 0.0
        first_role_ms: float | None = None
        first_reasoning_ms: float | None = None
        first_content_ms: float | None = None

        def frame(delta: dict, finish: str | None = None,
                  usage: dict | None = None, x_soda: dict | None = None,
                  error: dict | None = None) -> str:
            nonlocal frame_count, frame_bytes, last_frame_at, max_idle_gap
            nonlocal first_role_ms, first_reasoning_ms, first_content_ms
            choice = {"index": 0, "delta": delta, "finish_reason": finish}
            chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "choices": [choice],
            }
            if usage:
                chunk["usage"] = usage
            if x_soda:
                chunk["x_soda"] = x_soda
            if error:
                chunk["error"] = error
            rendered = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            now = time.monotonic()
            elapsed_ms = (now - request_started) * 1000
            if delta.get("role") and first_role_ms is None:
                first_role_ms = elapsed_ms
            if delta.get("reasoning") and first_reasoning_ms is None:
                first_reasoning_ms = elapsed_ms
            if delta.get("content") and first_content_ms is None:
                first_content_ms = elapsed_ms
            if last_frame_at is not None:
                max_idle_gap = max(max_idle_gap, now - last_frame_at)
            last_frame_at = now
            frame_count += 1
            frame_bytes += len(rendered.encode("utf-8"))
            return rendered

        # The first protocol frame is emitted before checkpoint restoration,
        # media fetching or any agent/tool work.
        yield frame({"role": "assistant"})

        # role_first performs the heavy import in a worker after the role frame;
        # other modes already loaded above (normally a prewarm cache hit).
        stack = agent_stack or await _load_agent_stack_async(threaded=True)
        loaded_chat_turn, render_skill_card, render_tool_card, render_search_table, skill_display_title = stack
        state: dict = {}

        async def produce() -> None:
            try:
                prepared = await prepare()
                state.update(prepared)
                async for ev in _chat_turn_stream(
                    loaded_chat_turn,
                    prepared["user_message"],
                    prepared["session"],
                    prepared["attachments"],
                    prepared["checkpoint_cb"],
                    progress_cb=progress_cb,
                    execution_context=execution,
                    system_instructions=prepared["caller_system"],
                ):
                    await queue.put(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await queue.put({"type": "error", "message": str(exc)})
            finally:
                await queue.put(None)

        task = asyncio.create_task(produce())
        artifact_kinds: set[str] = set()
        result_files: list[dict] = []
        tool_results: list[dict] = []
        done_event: dict | None = None
        content_sent = 0
        content_parts: list[str] = []
        card_spent = 0
        blocks_emitted = False
        truncated = False
        finished = False
        terminal_emitted = False
        deadline_hit = False
        pending: list[dict | None] = []

        def emit_content(piece: str) -> str:
            nonlocal content_sent, truncated
            if truncated or not piece:
                return ""
            if content_budget is not None and content_sent + len(piece) > content_budget:
                piece = piece[:max(0, content_budget - content_sent)]
                truncated = True
            if not piece:
                return ""
            content_sent += len(piece)
            content_parts.append(piece)
            return frame({"content": piece})

        def emit_block(text: str, *, required: bool = False) -> str:
            nonlocal card_spent, blocks_emitted
            if not text:
                return ""
            card_budget = None if content_budget is None else int(content_budget * 0.6)
            if not required and card_budget is not None and card_spent + len(text) > card_budget:
                return ""
            card_spent += len(text)
            separator = "" if not blocks_emitted else "\n---\n\n"
            blocks_emitted = True
            return emit_content(separator + text + "\n\n")

        def emit_mermaid(text: str) -> str:
            """Emit a complete Mermaid block or a short omission notice."""
            if not text:
                return ""
            notice = "研究图谱 Mermaid 正文因 max_tokens 预算不足已整块省略；附件仍按展示策略生成。"
            separator_len = 0 if not blocks_emitted else len("\n---\n\n")
            card_budget = None if content_budget is None else int(content_budget * 0.6)
            enough_optional = (
                (card_budget is None or card_spent + len(text) <= card_budget)
                and (content_budget is None
                     or content_sent + separator_len + len(text) + 2 <= content_budget)
            )
            return emit_block(text) if enough_optional else emit_block(notice, required=True)

        # Clients concatenate delta.reasoning chunks verbatim, so distinct
        # reasoning segments need explicit separators: model thinking and each
        # tool module become their own paragraphs, progress notices inside a
        # module get one line each.
        last_reasoning_kind: list[str | None] = [None]

        def emit_reasoning(text: str, kind: str) -> str:
            if not text:
                return ""
            last = last_reasoning_kind[0]
            if last is None:
                separator = ""
            elif kind == "thinking":
                separator = "" if last == "thinking" else "\n\n"
            elif kind == "module":
                separator = "\n\n"
            else:  # "line"
                separator = "\n" if last in ("module", "line") else "\n\n"
            last_reasoning_kind[0] = kind
            return frame({"reasoning": separator + text})

        def handle(ev: dict) -> str | None:
            nonlocal done_event, finished, terminal_emitted
            etype = ev.get("type")
            if etype == "thinking" and ev.get("is_delta"):
                return emit_reasoning(ev.get("content", ""), "thinking") or None
            if etype == "answer" and ev.get("is_delta"):
                return emit_content(ev.get("content", "")) or None
            if etype == "tool_progress":
                text = str(ev.get("message") or "").strip()
                return emit_reasoning(text, "line") or None
            if etype == "skill_loaded":
                title = skill_display_title(str(ev.get("name") or ""))
                out = emit_reasoning(f"📘 已加载技能《{title}》，按其工作流执行…", "module")
                policy = state.get("policy")
                if policy is not None and policy.skill_card_enabled:
                    out += emit_block(render_skill_card(title))
                return out or None
            if etype == "tool_start":
                name = ev.get("name", "")
                if name == "use_skill":
                    return None
                return emit_reasoning(_tool_progress_text(name, ev.get("args") or {}), "module") or None
            if etype == "tool_warning":
                return emit_reasoning(f"注意：{ev.get('warning', '')}", "line") or None
            if etype == "tool_result":
                result = ev.get("result") or {}
                tool = result.get("tool", "")
                if tool == "use_skill":
                    return None
                tool_results.append(result)
                if result.get("status") == "success":
                    files = _tool_result_files(request, result)
                    if files:
                        result_files.extend(files)
                    elif tool in _ARTIFACT_TOOLS:
                        # Compatibility fallback for older tool results that
                        # did not create their own export records.
                        artifact_kinds.add(tool)
                table = (render_search_table(result)
                         if tool == "search_papers" and result.get("status") != "error"
                         and state.get("table_allowed") else None)
                out = ""
                if table:
                    line = (render_tool_card(tool, result)
                            if state.get("cards_allowed") else "")
                    block = f"{line}\n\n{table}" if line else table
                    out += emit_block(block, required=True)
                elif state.get("cards_allowed") and (
                        result.get("status") != "error"
                        or (state.get("policy") is not None
                            and state["policy"].tool_error_cards_enabled)):
                    # Mirror of the non-streaming gate: error one-liners stay
                    # out of delta.content unless the administrator re-enabled
                    # them; the model still receives the raw tool result.
                    card = render_tool_card(tool, result)
                    if card:
                        out += emit_block(card)
                policy = state.get("policy")
                if tool == "research_map" and result.get("status") == "success" \
                        and policy is not None and policy.research_map_mermaid_enabled:
                    from tools.export.report import render_research_map_mermaid
                    out += emit_mermaid(render_research_map_mermaid(state["session"]))
                return out or None
            if etype == "done":
                done_event = ev
                finished = True
                return None
            if etype == "error":
                finished = True
                terminal_emitted = True
                return frame(
                    {}, finish="stop",
                    usage=_usage_of(done_event, est_prompt, content_sent // 4),
                    error={
                        "type": "upstream_error",
                        "message": ev.get("message", "unknown error"),
                    },
                )
            return None

        async def next_event() -> dict | None:
            if pending:
                return pending.pop(0)
            remaining = execution.remaining_soft()
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            timeout = min(_HEARTBEAT_SECONDS, remaining) if remaining is not None \
                else _HEARTBEAT_SECONDS
            return await asyncio.wait_for(queue.get(), timeout=timeout)

        async def coalesce(ev: dict) -> dict:
            etype = ev.get("type")
            if etype not in {"thinking", "answer"} or not ev.get("is_delta"):
                return ev
            text = str(ev.get("content") or "")
            deadline = loop.time() + _SSE_COALESCE_SECONDS
            while len(text) < _SSE_COALESCE_CHARS:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    nxt = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if nxt is None:
                    pending.append(None)
                    break
                if nxt.get("type") == etype and nxt.get("is_delta"):
                    text += str(nxt.get("content") or "")
                    continue
                pending.append(nxt)
                break
            return {**ev, "content": text}

        try:
            while True:
                try:
                    ev = await next_event()
                except asyncio.TimeoutError:
                    if execution.soft_expired():
                        deadline_hit = True
                        execution.cancel()
                        if not task.done():
                            task.cancel()
                        summaries = [
                            str(r.get("summary") or "").strip()[:180]
                            for r in tool_results
                            if r.get("status") in {"success", "partial"}
                            and str(r.get("summary") or "").strip()
                        ]
                        deadline_text = (
                            "本轮已达到平台交互时限，已保留完成的结果："
                            + "；".join(summaries[:3])
                            + "。其余增强步骤已停止，您可以在下一轮继续。"
                            if summaries else
                            "本轮已达到平台交互时限，耗时步骤已安全停止。"
                            "请缩小任务范围或在下一轮继续。"
                        )
                        yield emit_reasoning("已到达本轮时间预算，正在安全收尾…", "module")
                        out = emit_content(deadline_text)
                        if out:
                            yield out
                        done_event = {"deadline_exceeded": True, "usage": {}}
                        break
                    if await request.is_disconnected():
                        execution.cancel()
                        break
                    yield emit_reasoning("仍在处理中，请稍候…", "line")
                    continue
                if ev is None:
                    break
                ev = await coalesce(ev)
                out = handle(ev)
                if out is not None:
                    yield out
                if finished:
                    break

            session = state.get("session")
            attachments_out: list[dict] = []
            if session is not None and not deadline_hit:
                attachments_out = _dedupe_attachments(
                    _reports_for(
                        request, session, artifact_kinds,
                        research_map_svg_enabled=state["policy"].research_map_svg_enabled,
                        research_map_html_enabled=state["policy"].research_map_html_enabled,
                        research_map_markdown_enabled=state["policy"].research_map_markdown_enabled,
                    )
                    + result_files
                    + _extra_attachments(
                        request, session, tool_results,
                        bibtex_export_mode=state["policy"].bibtex_export_mode)
                )
            if session is not None and not deadline_hit and not terminal_emitted \
                    and _bibtex_md_note_needed(tool_results, state["policy"]):
                # Emitted through emit_content so the notice reaches both the
                # delta.content stream and content_parts before the checkpoint
                # finalize below — the alias chain must match what 清小搭 echoes
                # back next turn. Dropped automatically when the budget is
                # already exhausted.
                from tools.export.cards import BIBTEX_MD_EXPORT_NOTE
                out = emit_content("\n\n" + BIBTEX_MD_EXPORT_NOTE)
                if out:
                    yield out
            if not terminal_emitted and done_event is not None and \
                    not done_event.get("deadline_exceeded") and \
                    state.get("finalize_checkpoint") is not None:
                try:
                    await asyncio.wait_for(
                        state["finalize_checkpoint"]("".join(content_parts)),
                        timeout=3.0,
                    )
                except (asyncio.TimeoutError, Exception):
                    pass
            if not terminal_emitted:
                x_soda = {"attachments": attachments_out} if attachments_out else None
                yield frame(
                    {}, finish="length" if truncated else "stop",
                    usage=_usage_of(done_event, est_prompt, content_sent // 4),
                    x_soda=x_soda,
                )
            yield "data: [DONE]\n\n"
        finally:
            execution.cancel()
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            logger.info(
                "openai_stream_complete id=%s elapsed_ms=%d frames=%d bytes=%d "
                "max_idle_ms=%d first_role_ms=%s first_reasoning_ms=%s first_content_ms=%s deadline=%s cancelled=%s",
                cid,
                round((time.monotonic() - request_started) * 1000),
                frame_count,
                frame_bytes,
                round(max_idle_gap * 1000),
                round(first_role_ms, 1) if first_role_ms is not None else None,
                round(first_reasoning_ms, 1) if first_reasoning_ms is not None else None,
                round(first_content_ms, 1) if first_content_ms is not None else None,
                deadline_hit,
                execution.cancelled(),
            )

    return StreamingResponse(
        frame_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
