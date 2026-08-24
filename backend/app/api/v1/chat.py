"""Chat API: conversational agent with SSE streaming + chat history CRUD."""
import asyncio
import json
import logging
import sys
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app.api.v1.auth import current_user
from core.blocking import run_cpu_bound
from app.schemas.chat import (ChatRequest, ChatHistoryListResponse, ChatHistoryItem,
                              ChatRenameRequest)

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_UPLOAD_DIR = _PROJECT_ROOT / "data" / "uploads"
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB per file
_MAX_TEXT_CHARS = 12000  # cap injected upload text in LLM context (D-087)


def _extract_text(filename: str, raw: bytes) -> str:
    """Extract text from an uploaded file (D-087).

    Delegates to the shared ingest pipeline (tools/ingest/downloader):
    PDFs via the column-aware PyMuPDF parser, docx via python-docx,
    tex/txt/md as plain text. Returns "" on failure.
    """
    lower = filename.lower()
    ext = lower.rsplit(".", 1)[-1] if "." in lower else ""
    try:
        from tools.ingest.attachments import extract_quick_text
        return extract_quick_text(raw, ext)
    except Exception:
        return ""


def _owned_web_attachments(items: list[dict], user_id: str) -> list[dict]:
    """Return canonical attachment metadata restricted to one web owner."""
    import re as _re
    from core.web_artifact_store import owned_web_attachment, register_web_artifact
    from tools.ingest.attachments import find_original, text_path

    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "")
        if not _re.fullmatch(r"[a-f0-9]{32}", aid):
            raise HTTPException(404, "attachment not found")
        metadata = owned_web_attachment(aid, user_id)
        if metadata is None and user_id == "local":
            # Auth-disabled local development may reuse pre-index data created
            # before ownership tracking was introduced. It is still accepted
            # only when the id resolves to a real local upload, never by id
            # alone, and is immediately bound to the local identity.
            if find_original(aid) is None and not text_path(aid).is_file():
                raise HTTPException(404, "attachment not found")
            metadata = dict(item)
            register_web_artifact("attachment", aid, user_id, metadata)
        if metadata is None:
            raise HTTPException(404, "attachment not found")
        if find_original(aid) is None and not text_path(aid).is_file():
            raise HTTPException(404, "attachment not found")
        safe = {
            key: metadata.get(key, item.get(key, ""))
            for key in ("id", "filename", "char_count", "ext", "media_type",
                        "multimodal_status", "element_count", "preview_url")
        }
        safe["id"] = aid
        out.append(safe)
    return out


@router.post("/stream")
async def chat_stream(req: ChatRequest, authorization: str | None = Header(None),
                            x_guest_id: str | None = Header(None)):
    """SSE endpoint for conversational chat with tool-calling."""
    from agents.chat_agent import ChatSession, chat_turn, load_chat_history, save_chat_history
    from core.turn_execution import TurnExecutionContext

    user = current_user(authorization, x_guest_id)
    uid = user["id"]

    # Load or create session (another user's record loads as not-found).
    session = None
    if req.history_filename:
        session = load_chat_history(req.history_filename, user_id=uid)

    if session is None:
        session = ChatSession(
            topic=req.topic,
            conception=req.conception,
            language=req.language,
            field_profile=req.field_profile,
       )
    session.owner_id = uid
    request_attachments = _owned_web_attachments(req.attachments, uid)

    progress_queue: asyncio.Queue = asyncio.Queue()

    def progress_cb(msg: str):
        progress_queue.put_nowait(("progress", msg))

    execution = TurnExecutionContext(
        channel="web", soft_timeout_seconds=240.0, hard_timeout_seconds=300.0)

    async def event_stream():
        tool_call_active = False

        async def run_chat():
            nonlocal tool_call_active
            async for event in chat_turn(req.message, session, progress_cb,
                                          attachments=request_attachments,
                                          regenerate=req.regenerate,
                                          execution_context=execution):
                if event["type"] in ("tool_start",):
                    tool_call_active = True
                # D-071: accumulate trace_id so save_chat_history links the trace
                if event.get("type") == "done" and event.get("trace_id"):
                    if event["trace_id"] not in session.trace_ids:
                        session.trace_ids.append(event["trace_id"])
                await progress_queue.put(("event", event))

            # A soft deadline is a normal, saveable degraded completion.
            # Hard cancellation exits before this block and must not persist a
            # half-turn.
            if not execution.hard_expired() and not execution.cancelled():
                fname = save_chat_history(session, user_id=uid)
                await progress_queue.put(("saved", fname))

        task = asyncio.create_task(run_chat())
        elapsed = 0

        # D-088: wrap the stream loop in try/finally so that a client abort
        # (the user hit "stop") cancels the background run_chat task. Without
        # this, the task keeps running after the SSE connection closes — the
        # LLM stream + any in-flight tool call keep consuming tokens/time.
        # On cancel the turn is NOT saved, so history rolls back to the
        # pre-turn state (no orphan half-reply persisted).
        try:
            while True:
                try:
                    remaining_hard = execution.remaining_hard()
                    wait_seconds = min(15.0, remaining_hard) if remaining_hard is not None else 15.0
                    item = await asyncio.wait_for(progress_queue.get(), timeout=max(0.001, wait_seconds))
                    elapsed = 0
                except asyncio.TimeoutError:
                    if execution.hard_expired():
                        execution.cancel()
                        if not task.done():
                            task.cancel()
                        error = {"type": "deadline_exceeded", "message": "本轮已超过 300 秒硬时限，未保存半轮历史。"}
                        yield f"event: error\ndata: {json.dumps(error, ensure_ascii=False)}\n\n"
                        break
                    # Heartbeat: keep connection alive, show user we are still working
                    elapsed += 15
                    hb = {"type": "heartbeat", "elapsed": elapsed}
                    yield f"event: heartbeat\ndata: {json.dumps(hb, ensure_ascii=False)}\n\n"
                    continue

                kind, payload = item
                if kind == "event":
                    yield f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                elif kind == "progress":
                    msg = {"type": "tool_progress", "message": payload}
                    yield f"event: tool_progress\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
                elif kind == "saved":
                    done_data = {
                        "type": "history_saved",
                        "filename": Path(payload).name,
                        "filepath": payload,
                    }
                    yield f"event: history_saved\ndata: {json.dumps(done_data, ensure_ascii=False)}\n\n"
                    break

            try:
                await task
            except asyncio.CancelledError:
                if not execution.hard_expired():
                    raise
            logger.info("web_stream_complete elapsed_ms=%d phase=%s timeout_count=%d timeout_kind=%s cancelled=%s",
                        round(execution.elapsed() * 1000), execution.phase, execution.timeout_count,
                        execution.last_timeout_kind, execution.cancelled())
        finally:
            # Client disconnected / aborted: cancel the in-flight turn.
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/history", response_model=ChatHistoryListResponse)
def list_chat_history_records(authorization: str | None = Header(None),
                            x_guest_id: str | None = Header(None)):
    from agents.chat_agent import list_chat_history
    user = current_user(authorization, x_guest_id)
    records = list_chat_history(user_id=user["id"])
    return ChatHistoryListResponse(
        records=[
            ChatHistoryItem(
                filename=r["filename"],
                timestamp=r["timestamp"],
                topic=r["topic"],
                message_count=r.get("message_count", 0),
                title=r.get("title", "") or r.get("topic", ""),
                paper_count=r.get("paper_count", 0),
               has_review=r.get("has_review", False),
               has_map=r.get("has_map", False),
               trace_ids=r.get("trace_ids", []),
                source=r.get("source", "chat"),
           )
           for r in records
        ]
    )


@router.get("/history/{filename:path}")
def load_chat_record(filename: str, authorization: str | None = Header(None),
                            x_guest_id: str | None = Header(None)):
    from agents.chat_agent import load_chat_history
    user = current_user(authorization, x_guest_id)
    session = load_chat_history(filename, user_id=user["id"])
    if session is None:
        raise HTTPException(404, "Chat history not found")

    def _paper_dict(p):
        return {
            "id": p.id, "title": p.title, "authors": p.authors,
            "year": p.year, "venue": p.venue, "doi": p.doi,
            "source": p.source, "citation_count": p.citation_count,
            "abstract": p.abstract,
            "keywords": p.keywords, "urls": p.urls,
            "relevance_score": p.relevance_score, "layer": p.layer,
            "llm_reasoning": p.llm_reasoning,
        }

    # Defence for records written by older versions: internal skill loading
    # was once persisted as a visible tool card containing full instructions.
    # Never return that payload to the browser during history replay.
    public_messages = []
    for message in session.messages:
        item = dict(message)
        if isinstance(item.get("toolCalls"), list):
            item["toolCalls"] = [
                tc for tc in item["toolCalls"]
                if isinstance(tc, dict) and tc.get("name") != "use_skill"
            ]
        public_messages.append(item)

    return {
        "type": "chat",
        "topic": session.topic,
        "conception": session.conception,
        "language": session.language,
        "field_profile": session.field_profile,
        "messages": public_messages,
        "papers": [_paper_dict(p) for p in session.papers],
        "candidates": [_paper_dict(p) for p in session.candidates],
        "literature_review": session.literature_review,
        "review_metadata": getattr(session, "review_metadata", {}),
        "map_data": session.map_data,
        "reading_path": session.reading_path,
        "sub_directions": session.sub_directions,
        "search_queries": session.search_queries,
        "attachments": session.attachments,
        "paper_summaries": {pid: s if isinstance(s, dict) else {
            "paper_id": s.paper_id, "research_problem": s.research_problem,
            "key_findings": s.key_findings, "elements": s.elements,
            "section_outline": s.section_outline,
            "document_info": s.document_info,
        } for pid, s in session.paper_summaries.items()},
    }


@router.delete("/history/{filename:path}")
def delete_chat_record(filename: str, authorization: str | None = Header(None),
                            x_guest_id: str | None = Header(None)):
    """Delete a chat history record via the unified store (D-071)."""
    from agents.chat_agent import delete_chat_history
    user = current_user(authorization, x_guest_id)
    safe_name = Path(filename).name
    if not delete_chat_history(safe_name, user_id=user["id"]):
        raise HTTPException(404, "Chat history not found")
    return {"status": "deleted", "filename": safe_name}


@router.patch("/history/{filename:path}")
def rename_chat_record(filename: str, req: ChatRenameRequest,
                       authorization: str | None = Header(None),
                            x_guest_id: str | None = Header(None)):
    """Rename a chat history record's title (D-071). Filename unchanged."""
    from agents.chat_agent import rename_chat_history
    user = current_user(authorization, x_guest_id)
    safe_name = Path(filename).name
    if not rename_chat_history(safe_name, req.title, user_id=user["id"]):
        raise HTTPException(404, "Chat history not found")
    return {"status": "renamed", "filename": safe_name, "title": req.title}


from fastapi import UploadFile, File

@router.post("/upload")
async def upload_chat_files(files: list[UploadFile] = File(...),
                            authorization: str | None = Header(None),
                            x_guest_id: str | None = Header(None)):
    """Persist originals and fast-extracted text; defer all VLM work.

    Supported: PDF/DOCX/TEX/TXT/MD/BIB and PNG/JPG/WebP. Scanned PDFs and
    images are accepted with zero extracted characters and marked ``pending``;
    a later deep-read or modality question performs cached multimodal analysis.
    """
    from tools.ingest.attachments import AttachmentError, save_attachment

    user = current_user(authorization, x_guest_id)
    results: list[dict] = []
    for upload in files:
        raw = await upload.read()
        if len(raw) > _MAX_UPLOAD_BYTES:
            results.append({
                "filename": upload.filename, "id": "", "char_count": 0,
                "text_preview": "",
                "error": f"file too large (>{_MAX_UPLOAD_BYTES // (1024 * 1024)}MB)",
            })
            continue
        try:
            results.append(await run_cpu_bound(
                save_attachment, raw, upload.filename or "upload",
                content_type=upload.content_type or "", owner_id=user["id"],
            ))
        except AttachmentError as e:
            results.append({
                "filename": upload.filename, "id": "", "char_count": 0,
                "text_preview": "", "error": str(e),
            })
    return {"attachments": results}


@router.get("/file/{file_id}/raw")
def get_chat_file_raw(file_id: str, authorization: str | None = Header(None),
                      x_guest_id: str | None = Header(None)):
    """Serve a stored original through an id-only, extension-whitelisted lookup."""
    import re as _re
    from tools.ingest.attachments import IMAGE_EXTENSIONS, find_original, media_type_for

    user = current_user(authorization, x_guest_id)
    if not _re.fullmatch(r"[a-f0-9]{32}", file_id):
        raise HTTPException(400, "invalid file id")
    from core.web_artifact_store import owned_web_attachment
    metadata = owned_web_attachment(file_id, user["id"])
    if metadata is None and user["id"] == "local" and find_original(file_id) is not None:
        from core.web_artifact_store import register_web_artifact
        register_web_artifact("attachment", file_id, user["id"])
        metadata = {}
    if metadata is None:
        raise HTTPException(404, "file not found")
    fp = find_original(file_id)
    if fp is None:
        raise HTTPException(404, "file not found")
    ext = fp.suffix.lower().lstrip(".")
    # Browser-inline preview is deliberately restricted to safe raster images.
    if ext not in IMAGE_EXTENSIONS:
        raise HTTPException(400, "raw preview is only available for images")
    return FileResponse(fp, media_type=media_type_for(ext),
                        headers={"Content-Disposition": f'inline; filename="{file_id}.{ext}"'})


@router.get("/file/{file_id}")
def get_chat_file(file_id: str, authorization: str | None = Header(None),
                            x_guest_id: str | None = Header(None)):
    """Return the extracted text stored for an uploaded chat file (D-091).

    The chat upload endpoint (D-087) stores extracted text at
    data/uploads/<id>.txt and returns only lightweight metadata to the
    frontend. This endpoint lets the right-sidebar file viewer fetch the
    full text so a user can click a file chip and read its contents.
    `file_id` is the uuid hex returned by /chat/upload.
    """
    import re as _re
    user = current_user(authorization, x_guest_id)
    if not _re.fullmatch(r"[a-f0-9]{32}", file_id):
        raise HTTPException(400, "invalid file id")
    from core.web_artifact_store import owned_web_attachment
    metadata = owned_web_attachment(file_id, user["id"])
    if metadata is None and user["id"] == "local" and (_UPLOAD_DIR / f"{file_id}.txt").is_file():
        from core.web_artifact_store import register_web_artifact
        register_web_artifact("attachment", file_id, user["id"])
        metadata = {}
    if metadata is None:
        raise HTTPException(404, "file not found")
    fp = _UPLOAD_DIR / f"{file_id}.txt"
    if not fp.exists():
        raise HTTPException(404, "file not found")
    try:
        text = fp.read_text(encoding="utf-8")
    except OSError as e:
        # Don't echo the OS error (may leak server paths) to the client.
        raise HTTPException(500, "failed to read file")
    # Cap very large transcripts so the response stays snappy; the viewer
    # scrolls within the returned slice.
    _CAP = 200_000
    capped = len(text) > _CAP
    return {
        "id": file_id,
        "text": text[:_CAP] if capped else text,
        "char_count": len(text),
        "truncated": capped,
    }
