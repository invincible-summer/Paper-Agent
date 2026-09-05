"""阅研工作台：Web 会话内原文、批注、翻译与独立阅读讨论。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.v1.auth import current_user
from agents.session import ChatSession, load_chat_history, save_chat_history
from core import reading_store as store
from core.blocking import run_cpu_bound
from core.reading_pdf import manifest, page_evidence
from core.web_session_lock import locked_session, session_lock

router = APIRouter(prefix="/reader", tags=["reader"])
ID = Annotated[str, Field(pattern=r"^[a-f0-9]{16,32}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OpenRequest(StrictModel):
    history_filename: str | None = Field(default=None, max_length=255)
    attachment_id: ID


class Selection(StrictModel):
    page: int = Field(ge=1, le=100000)
    quote: str = Field(default="", max_length=6000)
    rects: list[list[float]] = Field(default_factory=list, max_length=100)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("rects")
    @classmethod
    def valid_rects(cls, value):
        for r in value:
            if len(r) != 4 or not all(0 <= n <= 1 for n in r) or r[0] > r[2] or r[1] > r[3]:
                raise ValueError("invalid normalized rectangle")
        return value


class Position(StrictModel):
    page: int = Field(ge=1, le=100000)
    zoom: float = Field(default=1, ge=.5, le=3)
    view: Literal["continuous", "single", "spread"] = "continuous"
    goal: str = Field(default="理解方法", max_length=500)
    glossary: str = Field(default="", max_length=4000)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_version: int = Field(default=0, ge=0)


class NoteRequest(StrictModel):
    anchor_id: ID
    category: Literal["highlight", "contribution", "evidence", "question", "method"] = "highlight"
    note: str = Field(default="", max_length=10000)
    interpretation: str = Field(default="", max_length=14000)
    expected_version: int = Field(default=0, ge=0)


class ActionRequest(StrictModel):
    action: Literal["translate", "ask"]
    anchor_id: ID
    question: str = Field(default="", max_length=3000)
    target_language: Literal["中文", "English"] = "中文"
    thread_id: ID | None = None
    request_id: ID


def owner(authorization: str | None = Header(None), x_guest_id: str | None = Header(None)) -> str:
    return current_user(authorization, x_guest_id)["id"]


def _session(uid: str, sid: str) -> ChatSession:
    filename = store.filename_for(uid, sid)
    session = load_chat_history(filename, user_id=uid) if filename else None
    if session is None or session.session_id != sid:
        raise HTTPException(404, "会话不存在")
    session.owner_id = uid
    return session


def _document(uid: str, sid: str, aid: str):
    from core.web_artifact_store import owned_web_attachment
    from tools.ingest.attachments import find_original
    session = _session(uid, sid)
    attachment = next((a for a in session.attachments if a.get("id") == aid), None)
    if attachment is None or owned_web_attachment(aid, uid) is None:
        raise HTTPException(404, "此会话中没有该文件")
    path = find_original(aid)
    if path is None or path.suffix.lower() != ".pdf":
        raise HTTPException(404, "PDF 原件不可用")
    return session, attachment, path


async def _manifest(path):
    try:
        return await run_cpu_bound(manifest, path)
    except Exception as exc:
        raise HTTPException(422, "PDF 无法读取，可能已损坏或需要密码，请上传可阅读的副本") from exc


def _put(*args, **kwargs):
    try:
        return store.put(*args, **kwargs)
    except store.ReadingConflict as exc:
        raise HTTPException(409, str(exc)) from exc


def _record(uid, sid, aid, kind, rid):
    value = store.get(uid, sid, aid, kind, rid)
    if value is None:
        raise HTTPException(404, "阅读记录不存在")
    return value


async def _anchor(uid, sid, aid, anchor_id, path):
    anchor = _record(uid, sid, aid, "anchor", anchor_id)
    if anchor["fingerprint"] != (await _manifest(path))["fingerprint"]:
        raise HTTPException(409, "原文件版本已改变，请重新选择原文")
    return anchor


@router.post("/open")
async def open_document(req: OpenRequest, uid: str = Depends(owner)):
    from app.api.v1.chat import _owned_web_attachments
    attachment = _owned_web_attachments([{"id": req.attachment_id}], uid)[0]
    if attachment.get("ext", "").lower() != "pdf":
        raise HTTPException(422, "第一版工作台支持 PDF，请选择 PDF 原件")
    async with locked_session(uid, req.history_filename or f"draft-{req.attachment_id}"):
        session = load_chat_history(req.history_filename, user_id=uid) if req.history_filename else None
        if req.history_filename and session is None:
            raise HTTPException(404, "会话不存在")
        if session is None:
            session = ChatSession(title=attachment.get("filename", "论文阅读"), owner_id=uid)
        if not any(a.get("id") == req.attachment_id for a in session.attachments):
            session.attachments.append({**attachment, "rag_index_pending": True})
        fname = save_chat_history(session, user_id=uid)
        store.bind(uid, session.session_id, fname)
    return {"session_id": session.session_id, "history_filename": fname, "attachment_id": req.attachment_id}


@router.get("/sessions/{sid}/documents/{aid}")
async def get_document(sid: ID, aid: ID, uid: str = Depends(owner)):
    session, attachment, path = _document(uid, sid, aid)
    info = await _manifest(path)
    return {**info, "attachment": attachment, "session_id": sid, "history_filename": session.history_filename,
            "title": session.title or session.topic or "论文阅读", "topic": session.topic,
            "documents": [a for a in session.attachments if a.get("ext", "").lower() == "pdf"],
            "position": store.get(uid, sid, aid, "position", "current"),
            "notes": store.list_records(uid, sid, aid, "note"),
            "anchors": store.list_records(uid, sid, aid, "anchor"),
            "threads": store.list_records(uid, sid, aid, "thread")}


@router.get("/sessions/{sid}/documents/{aid}/content")
async def get_content(sid: ID, aid: ID, uid: str = Depends(owner)):
    _, _, path = _document(uid, sid, aid)
    return FileResponse(path, media_type="application/pdf", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{aid}.pdf"',
    })


@router.put("/sessions/{sid}/documents/{aid}/position")
async def save_position(sid: ID, aid: ID, req: Position, uid: str = Depends(owner)):
    _, _, path = _document(uid, sid, aid)
    info = await _manifest(path)
    if req.page > info["page_count"] or req.fingerprint != info["fingerprint"]:
        raise HTTPException(409, "文档版本或页码已改变")
    return _put(uid, sid, aid, "position", req.model_dump(exclude={"expected_version"}),
                "current", req.expected_version)


@router.post("/sessions/{sid}/documents/{aid}/anchors")
async def create_anchor(sid: ID, aid: ID, req: Selection, uid: str = Depends(owner)):
    _, _, path = _document(uid, sid, aid)
    info = await _manifest(path)
    if req.page > info["page_count"] or req.fingerprint != info["fingerprint"]:
        raise HTTPException(409, "文档版本或页码已改变")
    evidence = await run_cpu_bound(page_evidence, path, req.page, req.quote, req.rects)
    evidence.pop("context")
    evidence["fingerprint"] = req.fingerprint
    if req.quote and not evidence["verified"]:
        raise HTTPException(422, "选文与该页文本未匹配，请重新选择；扫描页可使用页级提问")
    identity = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
    existing = store.get(uid, sid, aid, "anchor", identity)
    if existing:
        return existing
    if len(store.list_records(uid, sid, aid, "anchor")) >= 3000:
        raise HTTPException(409, "本篇阅读标记已达到上限")
    return _put(uid, sid, aid, "anchor", evidence, identity)


@router.post("/sessions/{sid}/documents/{aid}/notes")
async def create_note(sid: ID, aid: ID, req: NoteRequest, uid: str = Depends(owner)):
    _, _, path = _document(uid, sid, aid)
    await _anchor(uid, sid, aid, req.anchor_id, path)
    if len(store.list_records(uid, sid, aid, "note")) >= 1000:
        raise HTTPException(409, "本篇笔记已达到上限")
    return _put(uid, sid, aid, "note", req.model_dump(exclude={"expected_version"}))


@router.put("/sessions/{sid}/documents/{aid}/notes/{note_id}")
async def update_note(sid: ID, aid: ID, note_id: ID, req: NoteRequest, uid: str = Depends(owner)):
    _, _, path = _document(uid, sid, aid)
    old = _record(uid, sid, aid, "note", note_id)
    if old["anchor_id"] != req.anchor_id:
        raise HTTPException(422, "不能改写笔记的原始出处")
    await _anchor(uid, sid, aid, req.anchor_id, path)
    return _put(uid, sid, aid, "note", req.model_dump(exclude={"expected_version"}), note_id, req.expected_version)


@router.delete("/sessions/{sid}/documents/{aid}/notes/{note_id}")
async def delete_note(sid: ID, aid: ID, note_id: ID, version: int, uid: str = Depends(owner)):
    _document(uid, sid, aid)
    try:
        store.delete(uid, sid, aid, "note", note_id, version)
    except store.ReadingConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"deleted": True}


@router.post("/sessions/{sid}/documents/{aid}/notes/{note_id}/publish")
async def publish_note(sid: ID, aid: ID, note_id: ID, uid: str = Depends(owner)):
    session, attachment, path = _document(uid, sid, aid)
    async with locked_session(uid, session.history_filename):
        session = _session(uid, sid)
        note = _record(uid, sid, aid, "note", note_id)
        anchor = await _anchor(uid, sid, aid, note["anchor_id"], path)
        publication_id = f"reading:{note_id}:{note['version']}"
        if not any(m.get("publication_id") == publication_id for m in session.messages):
            link = f"/chat/{sid}/read/{aid}#anchor={anchor['id']}"
            quote = anchor["quote"].replace("\n", " ")
            content = (f"📖 阅读发现 · {attachment['filename']} · 第 {anchor['page']} 页\n\n"
                       + (f"> {quote}\n\n" if quote else "")
                       + (f"助读解释：{note['interpretation']}\n\n" if note.get("interpretation") else "")
                       + (f"我的笔记：{note['note']}\n\n" if note.get("note") else "")
                       + f"[返回原文]({link})")
            session.messages.append({"role": "user", "content": content, "publication_id": publication_id})
            save_chat_history(session, user_id=uid)
    return {"history_filename": session.history_filename, "published": True}


@router.post("/sessions/{sid}/documents/{aid}/actions")
async def reading_action(sid: ID, aid: ID, req: ActionRequest, uid: str = Depends(owner)):
    session, attachment, path = _document(uid, sid, aid)
    anchor = await _anchor(uid, sid, aid, req.anchor_id, path)
    if req.action == "translate" and not anchor["quote"]:
        raise HTTPException(422, "请先选择要翻译的原文")
    if req.action == "ask" and not req.question.strip():
        raise HTTPException(422, "请输入阅读问题")
    if req.thread_id:
        thread = _record(uid, sid, aid, "thread", req.thread_id)
        if thread["anchor_id"] != req.anchor_id:
            raise HTTPException(422, "此讨论属于另一段原文")
    else:
        thread = None
    queue: asyncio.Queue = asyncio.Queue()
    signature = hashlib.sha256(req.model_dump_json().encode()).hexdigest()

    async def run():
        from core.llm import ainvoke_utility, get_llm
        from core.prompts.reading import ASK, TRANSLATE
        from langchain_core.messages import HumanMessage, SystemMessage
        try:
            # 请求去重同时涵盖重试和多个标签页。
            async with locked_session(uid, f"reader-action-{sid}-{req.request_id}"):
                previous = store.get(uid, sid, aid, "action", req.request_id)
                if previous:
                    if previous.get("request_signature") != signature:
                        raise ValueError("请求编号已用于其他操作")
                    if previous.get("thread_id"):
                        previous["thread"] = _record(uid, sid, aid, "thread", previous["thread_id"])
                    await queue.put({"type": "done", **previous})
                    return
                evidence = await run_cpu_bound(page_evidence, path, anchor["page"], anchor["quote"])
                position = store.get(uid, sid, aid, "position", "current") or {}
                committed = False
                if req.action == "translate":
                    await queue.put({"type": "step", "step": "结合语境翻译"})
                    payload = {"selected_text": anchor["quote"], "page_context": evidence["context"],
                               "paper": attachment["filename"], "topic": session.topic[:1000],
                               "target_language": req.target_language, "glossary": position.get("glossary", "")}
                    model = get_llm("light")
                    cache_id = hashlib.sha256(json.dumps({"payload": payload, "fingerprint": anchor["fingerprint"],
                        "model": str(getattr(model, "model", "unknown")), "prompt_version": TRANSLATE.version},
                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
                    async with locked_session(uid, f"translation-{sid}-{aid}-{cache_id}"):
                        cached = store.get(uid, sid, aid, "translation", cache_id)
                        if cached:
                            answer = cached["answer"]
                        else:
                            response = await ainvoke_utility(model, [SystemMessage(content=TRANSLATE.text),
                                HumanMessage(content=json.dumps(payload, ensure_ascii=False))], max_tokens=3000)
                            answer = str(response.content)
                            if not answer.strip():
                                raise ValueError("模型没有返回译文")
                            _document(uid, sid, aid)
                            _put(uid, sid, aid, "translation", {"answer": answer}, cache_id)
                            for old in store.list_records(uid, sid, aid, "translation")[:-100]:
                                store.delete(uid, sid, aid, "translation", old["id"], old["version"])
                    result = {"answer": answer, "anchor_id": req.anchor_id, "action": req.action}
                else:
                    from agents.orchestrator import chat_turn
                    from core.turn_execution import TurnExecutionContext
                    if session_lock(uid, session.history_filename).locked():
                        await queue.put({"type": "step", "step": "等待当前对话完成，可随时取消"})
                    async with locked_session(uid, session.history_filename):
                        fresh = _session(uid, sid)
                        current = _record(uid, sid, aid, "thread", req.thread_id) if req.thread_id else None
                        history = current.get("messages", []) if current else []
                        if len(history) >= 80:
                            raise ValueError("此讨论已达到 40 轮，请为该选区开启新讨论")
                        branch = ChatSession(session_id=sid, owner_id=uid, topic=fresh.topic,
                                             conception=fresh.conception, language=fresh.language,
                                             field_profile=fresh.field_profile, messages=copy.deepcopy(history),
                                             attachments=[copy.deepcopy(next(a for a in fresh.attachments if a.get("id") == aid))])
                        # 在相同 RAG session 中按附件建索引，纯阅读不触发。
                        indexed = store.get(uid, sid, aid, "index", "current")
                        if not indexed or indexed.get("fingerprint") != anchor["fingerprint"]:
                            from agents.orchestrator import _index_attachments
                            await run_cpu_bound(_index_attachments, branch, branch.attachments)
                            branch.attachments[0]["rag_index_pending"] = False
                            _put(uid, sid, aid, "index", {"fingerprint": anchor["fingerprint"]},
                                 "current", indexed["version"] if indexed else 0)
                        context = ASK.text + "\n阅读目标：" + position.get("goal", "理解方法")
                        prompt = (f"当前附件：{aid}；PDF 第 {anchor['page']} 页。\n"
                                  f"选文：{anchor['quote']}\n页内上下文：{evidence['context']}\n"
                                  f"我的问题：{req.question}")
                        answer, thinking, completed = "", "", False
                        async for event in chat_turn(prompt, branch, system_instructions=context,
                                allowed_tools=frozenset({"ask_papers", "deep_read", "explain_element", "exhibit_index"}),
                                execution_context=TurnExecutionContext(channel="web", soft_timeout_seconds=100, hard_timeout_seconds=120)):
                            if event["type"] == "error":
                                raise ValueError("助读暂不可用，请稍后重试")
                            if event["type"] == "answer":
                                answer += str(event.get("content", ""))
                            if event["type"] == "thinking":
                                thinking += str(event.get("content", ""))
                            if event["type"] == "done":
                                answer = event.get("answer") or answer
                                completed = bool(answer) and not event.get("deadline_exceeded")
                                if event.get("trace_id") and event["trace_id"] not in fresh.trace_ids:
                                    fresh.trace_ids.append(event["trace_id"])
                            elif event["type"] in {"answer", "thinking", "step", "tool_progress"}:
                                await queue.put(event)
                        if not completed:
                            raise ValueError("本次回答未完成，讨论未保存，可重试")
                        # 仅存阅读消息，拒绝把页内全文复制到持久历史。
                        messages = history + [{"role": "user", "content": req.question},
                                              {"role": "assistant", "content": answer[:24000], "thinking": thinking[:24000]}]
                        thread_payload = {"anchor_id": req.anchor_id,
                                          "title": (current or {}).get("title") or req.question[:80], "messages": messages}
                        # 只合并本篇附件的理解状态，不回写分支消息和研究产物。
                        if branch.attachments:
                            for a in fresh.attachments:
                                if a.get("id") == aid:
                                    a.update(branch.attachments[0])
                            save_chat_history(fresh, user_id=uid)
                        _document(uid, sid, aid)
                        result = store.finish_action(uid, sid, aid, req.request_id,
                            {"answer": answer, "anchor_id": req.anchor_id, "action": req.action,
                             "request_signature": signature}, thread_payload,
                            req.thread_id, current["version"] if current else 0)
                        committed = True
                _document(uid, sid, aid)  # 等待模型期间会话/账号可能已删除。
                if not committed:
                    result["request_signature"] = signature
                    result = store.finish_action(uid, sid, aid, req.request_id, result)
                # 仅保留最近 100 个重试结果，避免翻译缓存无限增长。
                for old in store.list_records(uid, sid, aid, "action")[:-100]:
                    store.delete(uid, sid, aid, "action", old["id"], old["version"])
                await queue.put({"type": "done", **result})
        except asyncio.CancelledError:
            raise
        except Exception:
            await queue.put({"type": "error", "message": "助读未完成，请重试；原文和已保存笔记不受影响。"})
        finally:
            await queue.put(None)

    async def events():
        task = asyncio.create_task(run())
        try:
            async with asyncio.timeout(150):
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), 10)
                    except asyncio.TimeoutError:
                        event = {"type": "heartbeat"}
                    if event is None:
                        break
                    yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except TimeoutError:
            yield 'event: error\ndata: {"message":"等待超时，请稍后重试。"}\n\n'
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
