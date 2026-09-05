"""阅读工作台的权限、原文证据、版本冲突及对话隔离。普通测试不调用模型。"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    import fitz
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.v1 import reader
    from core import history_store, reading_store, web_artifact_store
    from tools.ingest import attachments
    monkeypatch.setattr(history_store, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(reading_store, "READING_DB", tmp_path / "reading.db")
    monkeypatch.setattr(web_artifact_store, "WEB_ARTIFACT_DB", tmp_path / "owners.db")
    monkeypatch.setattr(attachments, "UPLOAD_DIR", tmp_path / "uploads")
    identity = {"id": "alice"}
    monkeypatch.setattr(reader, "current_user", lambda *_: identity)
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 90), "Our model may improve accuracy under fixed conditions.")
        page.insert_text((72, 120), "Table 1 reports 42.5 percent. See reference [3].")
        doc.new_page()
        pdf = doc.tobytes()
    rec = attachments.save_attachment(pdf, "reading.pdf", owner_id="alice")
    app = FastAPI()
    app.include_router(reader.router, prefix="/api/v1")
    with TestClient(app) as client:
        opened = client.post("/api/v1/reader/open", json={"attachment_id": rec["id"]})
        assert opened.status_code == 200, opened.text
        binding = opened.json()
        base = f"/api/v1/reader/sessions/{binding['session_id']}/documents/{rec['id']}"
        info = client.get(base).json()
        yield SimpleNamespace(client=client, base=base, info=info, identity=identity,
                              binding=binding, rec=rec, tmp=tmp_path)


def anchor(w, quote="Our model may improve accuracy under fixed conditions.", page=1):
    result = w.client.post(w.base + "/anchors", json={"page": page, "quote": quote,
                                                   "fingerprint": w.info["fingerprint"]})
    assert result.status_code == 200, result.text
    return result.json()


def test_pdf_opens_without_models_and_enforces_account_session_scope(workbench):
    w = workbench
    response = w.client.get(w.base + "/content")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
    assert w.info["page_count"] == 2
    assert "no-store" in response.headers["cache-control"]
    w.identity["id"] = "bob"
    assert w.client.get(w.base).status_code == 404
    assert w.client.get(w.base + "/content").status_code == 404
    assert w.client.post("/api/v1/reader/open", json={"attachment_id": w.rec["id"]}).status_code == 404
    w.identity["id"] = "alice"
    assert w.client.get(w.base.replace(w.binding["session_id"], "f" * 16)).status_code == 404


def test_exact_quote_verified_and_unmatched_quote_rejected(workbench):
    w = workbench
    a = anchor(w)
    assert a["verified"] and a["precision"] == "text" and a["rects"]
    assert anchor(w)["id"] == a["id"]
    forged = w.client.post(w.base + "/anchors", json={"page": 1, "quote": "We guarantee 100 percent.",
                                                     "fingerprint": w.info["fingerprint"]})
    assert forged.status_code == 422
    scanned = anchor(w, "", 2)
    assert not scanned["verified"] and scanned["precision"] == "page"


def test_anchor_rejects_stale_document_and_invalid_rectangles(workbench):
    w = workbench
    body = {"page": 1, "quote": "", "fingerprint": "a" * 64}
    assert w.client.post(w.base + "/anchors", json=body).status_code == 409
    body.update(fingerprint=w.info["fingerprint"], rects=[[float("inf"), 0, 1, 1]])
    body["rects"] = [[0, 0, 2, 1]]
    assert w.client.post(w.base + "/anchors", json=body).status_code == 422


def test_position_and_note_optimistic_conflicts_preserve_newer_value(workbench):
    w = workbench
    pos = {"page": 2, "fingerprint": w.info["fingerprint"], "glossary": "accuracy = 准确率"}
    assert w.client.put(w.base + "/position", json=pos).status_code == 200
    assert w.client.put(w.base + "/position", json=pos).status_code == 409
    a = anchor(w)
    data = {"anchor_id": a["id"], "note": "only fixed conditions", "category": "question"}
    note = w.client.post(w.base + "/notes", json=data).json()
    changed = {**data, "note": "check varying conditions", "expected_version": note["version"]}
    assert w.client.put(w.base + "/notes/" + note["id"], json=changed).status_code == 200
    assert w.client.put(w.base + "/notes/" + note["id"], json=changed).status_code == 409
    assert w.client.get(w.base).json()["notes"][0]["note"] == changed["note"]
    assert w.client.delete(w.base + f"/notes/{note['id']}?version=1").status_code == 409
    assert w.client.delete(w.base + f"/notes/{note['id']}?version=2").status_code == 200


def test_publish_is_idempotent_and_keeps_original_anchor(workbench):
    from agents.session import load_chat_history
    w = workbench
    a = anchor(w)
    note = w.client.post(w.base + "/notes", json={"anchor_id": a["id"], "note": "My judgement"}).json()
    url = w.base + f"/notes/{note['id']}/publish"
    assert w.client.post(url).status_code == 200
    assert w.client.post(url).status_code == 200
    session = load_chat_history(w.binding["history_filename"], user_id="alice")
    assert len(session.messages) == 1
    assert "My judgement" in session.messages[0]["content"]
    assert "#anchor=" + a["id"] in session.messages[0]["content"]
    assert session.messages[0]["role"] == "user"


def test_delete_session_cascades_reading_records(workbench, monkeypatch):
    from agents.session import delete_chat_history
    from core import reading_store
    from tools.storage.vectorstore import VectorStore
    monkeypatch.setattr(VectorStore, "__init__", lambda *_a, **_k: None)
    monkeypatch.setattr(VectorStore, "delete_session", lambda *_a: None)
    w = workbench
    a = anchor(w)
    w.client.post(w.base + "/notes", json={"anchor_id": a["id"], "note": "private"})
    assert delete_chat_history(w.binding["history_filename"], user_id="alice")
    assert reading_store.filename_for("alice", w.binding["session_id"]) is None
    assert reading_store.list_records("alice", w.binding["session_id"], w.rec["id"], "note") == []


def sse_events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_translation_has_context_is_bounded_and_does_not_write_chat(workbench, monkeypatch):
    import core.llm as llm
    from agents.session import load_chat_history
    w = workbench
    seen = []
    async def translate(model, messages, **kwargs):
        seen.append((messages, kwargs))
        return SimpleNamespace(content="在固定条件下，我们的模型可能提高准确率。")
    monkeypatch.setattr(llm, "get_llm", lambda *_: object())
    monkeypatch.setattr(llm, "ainvoke_utility", translate)
    a = anchor(w)
    req = {"action": "translate", "anchor_id": a["id"], "request_id": "b" * 32}
    response = w.client.post(w.base + "/actions", json=req)
    done = next(e for e in sse_events(response) if e["type"] == "done")
    assert "可能" in done["answer"]
    assert seen[0][1]["max_tokens"] == 3000
    assert "fixed conditions" in seen[0][0][1].content
    w.client.post(w.base + "/actions", json=req)
    assert len(seen) == 1
    assert load_chat_history(w.binding["history_filename"], user_id="alice").messages == []


def test_reading_question_uses_separate_thread_and_restricted_orchestrator(workbench, monkeypatch):
    import agents.orchestrator as orchestrator
    from agents.session import load_chat_history
    w = workbench
    seen = []
    async def turn(prompt, session, **kwargs):
        seen.append((prompt, session, kwargs))
        session.messages.append({"role": "user", "content": "must not leak into main"})
        yield {"type": "answer", "content": "Only under fixed conditions. [第 1 页](#page=1)"}
        yield {"type": "done", "answer": "Only under fixed conditions. [第 1 页](#page=1)"}
    monkeypatch.setattr(orchestrator, "chat_turn", turn)
    monkeypatch.setattr(orchestrator, "_index_attachments", lambda *_: None)
    a = anchor(w)
    req = {"action": "ask", "anchor_id": a["id"], "request_id": "c" * 32, "question": "When does it apply?"}
    response = w.client.post(w.base + "/actions", json=req)
    events = sse_events(response)
    done = next(e for e in events if e["type"] == "done")
    assert done["thread"]["messages"][0]["content"] == req["question"]
    assert "search_papers" not in seen[0][2]["allowed_tools"]
    assert seen[0][1].papers == []
    assert load_chat_history(w.binding["history_filename"], user_id="alice").messages == []
    assert len(w.client.get(w.base).json()["threads"]) == 1


def test_failed_model_leaves_reading_and_chat_usable(workbench, monkeypatch):
    import core.llm as llm
    w = workbench
    def unavailable(*_):
        raise RuntimeError("private provider detail must not appear")
    monkeypatch.setattr(llm, "get_llm", unavailable)
    a = anchor(w)
    response = w.client.post(w.base + "/actions", json={"action": "translate", "anchor_id": a["id"], "request_id": "d" * 32})
    assert "private provider" not in response.text
    assert any(e["type"] == "error" for e in sse_events(response))
    assert w.client.get(w.base + "/content").status_code == 200
    assert w.client.post(w.base + "/notes", json={"anchor_id": a["id"], "note": "still works"}).status_code == 200


def test_session_lock_serializes_same_history_but_not_other_users():
    from core.web_session_lock import locked_session, session_lock
    async def scenario():
        assert session_lock("alice", "x.json") is not session_lock("bob", "x.json")
        order = []
        async def first():
            async with locked_session("alice", "x.json"):
                order.append("start")
                await asyncio.sleep(.02)
                order.append("end")
        async def second():
            await asyncio.sleep(.005)
            async with locked_session("alice", "x.json"):
                order.append("second")
        await asyncio.gather(first(), second())
        assert order == ["start", "end", "second"]
    asyncio.run(scenario())


def test_thread_and_action_are_committed_together_and_retry_payload_is_small(workbench):
    from core import reading_store as store
    w = workbench
    args = ("alice", w.binding["session_id"], w.rec["id"])
    result = store.finish_action(*args, "e" * 32, {"answer": "a", "request_signature": "sig"},
                                 {"messages": [{"role": "user", "content": "question"}], "anchor_id": "a" * 32})
    assert result["thread"]["version"] == 1
    cached = store.get(*args, "action", "e" * 32)
    assert "thread" not in cached and cached["thread_id"] == result["thread"]["id"]
    with pytest.raises(store.ReadingConflict):
        store.finish_action(*args, "e" * 32, {"answer": "duplicate"},
                            {"messages": [{"role": "user", "content": "must roll back"}]},
                            result["thread"]["id"], 1)
    assert store.get(*args, "thread", result["thread"]["id"])["version"] == 1


def test_translation_cache_respects_glossary_and_rejects_request_reuse(workbench, monkeypatch):
    import core.llm as llm
    w = workbench
    calls = []
    async def translate(_model, messages, **_kwargs):
        calls.append(messages)
        return SimpleNamespace(content="bounded translation")
    monkeypatch.setattr(llm, "get_llm", lambda *_: SimpleNamespace(model="test"))
    monkeypatch.setattr(llm, "ainvoke_utility", translate)
    a = anchor(w)
    base = {"action": "translate", "anchor_id": a["id"]}
    for char in "ab":
        response = w.client.post(w.base + "/actions", json={**base, "request_id": char * 32})
        assert any(e["type"] == "done" for e in sse_events(response))
    assert len(calls) == 1
    pos = {"page": 1, "fingerprint": w.info["fingerprint"], "glossary": "model = 模型系统"}
    w.client.put(w.base + "/position", json=pos)
    w.client.post(w.base + "/actions", json={**base, "request_id": "c" * 32})
    assert len(calls) == 2
    response = w.client.post(w.base + "/actions", json={**base, "request_id": "c" * 32, "target_language": "English"})
    assert any(e["type"] == "error" for e in sse_events(response))
    assert len(calls) == 2


def test_cancelled_reading_does_not_commit_partial_thread(workbench, monkeypatch):
    import agents.orchestrator as orchestrator
    from app.api.v1 import reader
    from core import reading_store as store
    w = workbench
    a = anchor(w)
    cancelled = []
    async def turn(*_args, **_kwargs):
        try:
            yield {"type": "answer", "content": "partial"}
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(orchestrator, "chat_turn", turn)
    monkeypatch.setattr(orchestrator, "_index_attachments", lambda *_: None)
    async def scenario():
        response = await reader.reading_action(w.binding["session_id"], w.rec["id"], reader.ActionRequest(
            action="ask", anchor_id=a["id"], question="cancel me", request_id="f" * 32), "alice")
        stream = response.body_iterator
        first = await asyncio.wait_for(anext(stream), 5)
        assert "partial" in first
        await stream.aclose()
    asyncio.run(scenario())
    assert cancelled
    assert store.list_records("alice", w.binding["session_id"], w.rec["id"], "thread") == []
    assert store.get("alice", w.binding["session_id"], w.rec["id"], "action", "f" * 32) is None


def test_publication_waits_for_chat_write_then_reloads_latest_history(workbench):
    from app.api.v1 import reader
    from core.web_session_lock import locked_session
    from agents.session import load_chat_history, save_chat_history
    w = workbench
    a = anchor(w)
    note = w.client.post(w.base + "/notes", json={"anchor_id": a["id"], "note": "reading finding"}).json()
    async def scenario():
        async with locked_session("alice", w.binding["history_filename"]):
            task = asyncio.create_task(reader.publish_note(w.binding["session_id"], w.rec["id"], note["id"], "alice"))
            await asyncio.sleep(.02)
            assert not task.done()
            session = load_chat_history(w.binding["history_filename"], user_id="alice")
            session.messages.append({"role": "assistant", "content": "concurrent chat result"})
            save_chat_history(session, user_id="alice")
        await task
    asyncio.run(scenario())
    messages = load_chat_history(w.binding["history_filename"], user_id="alice").messages
    assert len(messages) == 2 and messages[0]["content"] == "concurrent chat result"
    assert "reading finding" in messages[1]["content"]


def test_record_ids_do_not_cross_documents_or_sessions(workbench):
    from agents.session import ChatSession, save_chat_history
    from core import reading_store
    w = workbench
    a = anchor(w)
    other = ChatSession(attachments=[w.rec])
    filename = save_chat_history(other, user_id="alice")
    reading_store.bind("alice", other.session_id, filename)
    url = w.base.replace(w.binding["session_id"], other.session_id)
    assert w.client.get(url).status_code == 200  # 同账号显式绑定的同一原件。
    assert w.client.post(url + "/notes", json={"anchor_id": a["id"], "note": "wrong scope"}).status_code == 404
    assert w.client.post(url + "/actions", json={"action": "translate", "anchor_id": a["id"], "request_id": "a" * 32}).status_code == 404


def test_restricted_orchestrator_rejects_unadvertised_tool_and_indexes_bound_upload(workbench, monkeypatch):
    import agents.orchestrator as orchestrator
    from agents.session import load_chat_history
    w = workbench
    calls, tool_names = [], []
    class Model:
        model = "reader-test"
        count = 0
        def bind_tools(self, tools):
            tool_names.extend(t.name for t in tools)
            return self
        async def astream(self, *_args, **_kwargs):
            self.count += 1
            yield SimpleNamespace(content="" if self.count == 1 else "done", additional_kwargs={}, usage_metadata=None,
                tool_call_chunks=[{"index": 0, "name": "search_papers", "args": '{"topic":"forbidden"}'}] if self.count == 1 else [])
    monkeypatch.setattr(orchestrator, "get_llm", lambda *_: Model())
    monkeypatch.setattr(orchestrator, "_index_attachments", lambda s, attachments: calls.append("index"))
    async def execute(*_args, **_kwargs):
        pytest.fail("tool outside allowed scope must never execute")
    monkeypatch.setattr(orchestrator, "execute_tool", execute)
    session = load_chat_history(w.binding["history_filename"], user_id="alice")
    async def scenario():
        return [e async for e in orchestrator.chat_turn("read", session, allowed_tools=frozenset({"ask_papers"}))]
    events = asyncio.run(scenario())
    assert tool_names == ["ask_papers"] and calls == ["index"]
    assert not session.attachments[0]["rag_index_pending"]
    assert any(e["type"] == "done" for e in events)


def test_region_selection_preserves_coordinates_without_claiming_verified_text(workbench):
    w = workbench
    rects = [[0.1, 0.2, 0.7, 0.6]]
    response = w.client.post(w.base + "/anchors", json={"page": 1, "quote": "", "rects": rects,
                                                       "fingerprint": w.info["fingerprint"]})
    assert response.status_code == 200
    region = response.json()
    assert region["rects"] == rects and region["precision"] == "region"
    assert not region["verified"] and region["quote"] == ""
    assert region["id"] != anchor(w, "", 1)["id"]
    note = w.client.post(w.base + "/notes", json={"anchor_id": region["id"], "note": "图表待核对"})
    assert note.status_code == 200
    assert any(a["id"] == region["id"] and a["rects"] == rects for a in w.client.get(w.base).json()["anchors"])
    assert w.client.post(w.base + "/actions", json={"action": "translate", "anchor_id": region["id"], "request_id": "a" * 32}).status_code == 422
