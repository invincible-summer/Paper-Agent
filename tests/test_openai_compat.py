"""Tests for the OpenAI-compatible (清小搭) endpoint + multimodal parsing
+ session memory. All offline: chat_turn is stubbed; HTTP via ASGI transport.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import agents.orchestrator as orch
from app.main import create_app


# --- helpers -----------------------------------------------------------------

def _stub_chat_turn(events):
    async def fake(user_message, session, progress_cb, attachments=None, regenerate=False):
        for ev in events:
            yield ev
    return fake


def _simple_events(answer="你好！有什么可以帮你？"):
    return [
        {"type": "step", "step": "thinking"},
        {"type": "thinking", "content": "用户在打招呼", "is_delta": True},
        {"type": "answer", "content": answer, "is_delta": True},
        {"type": "done", "thinking": "用户在打招呼", "answer": answer,
         "tool_calls": [], "trace_id": "t1",
         "usage": {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16}},
    ]


async def _post(client, body, key="sk-test"):
    return await client.post(
        "/v1/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {key}"},
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    import httpx
    from core.api_checkpoint import _STORE_CACHE

    monkeypatch.setenv("AGENT_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_STORAGE_ROOT", str(tmp_path / "openai-api"))
    _STORE_CACHE.clear()
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# --- auth + models ------------------------------------------------------------

@pytest.mark.anyio
async def test_models_ok_and_auth(client):
    resp = await client.get("/v1/models", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "paper-agent"


@pytest.mark.anyio
async def test_auth_401(client):
    resp = await client.get("/v1/models")
    assert resp.status_code == 401
    resp = await client.post("/v1/chat/completions", json={"messages": []})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_auth_enforced_when_key_set(monkeypatch):
    import httpx
    monkeypatch.setenv("AGENT_API_KEY", "sk-secret")
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/v1/models", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await c.get("/v1/models", headers={"Authorization": "Bearer sk-secret"})).status_code == 200


# --- non-streaming ------------------------------------------------------------

@pytest.mark.anyio
async def test_nonstream_response_shape(client, monkeypatch):
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(_simple_events()))
    resp = await _post(client, {"messages": [{"role": "user", "content": "你好"}]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"] == "你好！有什么可以帮你？"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["total_tokens"] == 16


@pytest.mark.anyio
async def test_nonstream_max_tokens_length(client, monkeypatch):
    monkeypatch.setattr(orch, "chat_turn",
                        _stub_chat_turn(_simple_events(answer="x" * 100)))
    resp = await _post(client, {"max_tokens": 1,
                                "messages": [{"role": "user", "content": "hi"}]})
    data = resp.json()
    assert data["choices"][0]["finish_reason"] == "length"
    assert len(data["choices"][0]["message"]["content"]) <= 4


@pytest.mark.anyio
async def test_nonstream_error_before_content_returns_500(client, monkeypatch):
    monkeypatch.setattr(orch, "chat_turn",
                        _stub_chat_turn([{"type": "error", "message": "LLM down"}]))
    resp = await _post(client, {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 500
    assert resp.json()["error"]["type"] == "upstream_error"


# --- streaming ----------------------------------------------------------------

def _parse_sse(text: str) -> list[dict]:
    frames = []
    for block in text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: ") and block != "data: [DONE]":
            frames.append(json.loads(block[len("data: "):]))
    return frames


@pytest.mark.anyio
async def test_stream_frame_sequence(client, monkeypatch):
    events = [
        {"type": "step", "step": "thinking"},
        {"type": "thinking", "content": "想想", "is_delta": True},
        {"type": "tool_start", "name": "search_papers", "args": {"topic": "gnn"}},
        {"type": "answer", "content": "结果", "is_delta": True},
        {"type": "answer", "content": "如下", "is_delta": True},
        {"type": "done", "thinking": "想想", "answer": "结果如下", "tool_calls": [],
         "trace_id": "t",
         "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
    ]
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(events))
    resp = await _post(client, {"stream": True, "max_tokens": 100,
                                "messages": [{"role": "user", "content": "gnn"}]})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    text = resp.text
    assert text.rstrip().endswith("data: [DONE]")

    frames = _parse_sse(text)
    # 1) role frame first, exactly once
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    # 2) reasoning frames (thinking + tool progress)
    reasonings = [f["choices"][0]["delta"].get("reasoning") for f in frames]
    assert "想想" in reasonings
    assert any(r and "检索论文" in r for r in reasonings)
    # 3) content frames
    contents = [f["choices"][0]["delta"].get("content") for f in frames]
    assert "结果" in contents and "如下" in contents
    # 4) final stop frame with usage, whitelist finish_reason
    stop = frames[-1]["choices"][0]
    assert stop["finish_reason"] in ("stop", "length", "tool_calls",
                                     "content_filter", "function_call")
    assert frames[-1]["usage"]["total_tokens"] == 7


@pytest.mark.anyio
async def test_stream_hides_use_skill_and_unknown_tool_names(client, monkeypatch):
    events = [
        {"type": "tool_start", "name": "use_skill", "args": {"name": "secret_skill"}},
        {"type": "tool_result", "result": {"tool": "use_skill", "status": "success",
                                             "instructions": "private"}},
        {"type": "tool_start", "name": "internal_router", "args": {"x": 1}},
        {"type": "answer", "content": "完成", "is_delta": True},
        {"type": "done", "thinking": "", "answer": "完成", "tool_calls": [],
         "trace_id": "t", "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                      "total_tokens": 2}},
    ]
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(events))
    resp = await _post(client, {"stream": True,
                                "messages": [{"role": "user", "content": "go"}]})
    assert resp.status_code == 200
    assert "secret_skill" not in resp.text
    assert "private" not in resp.text
    assert "internal_router" not in resp.text
    assert "正在调用工具" not in resp.text
    assert "正在继续处理当前任务" in resp.text


@pytest.mark.anyio
async def test_stream_mid_error_frame(client, monkeypatch):
    events = [
        {"type": "thinking", "content": "开始", "is_delta": True},
        {"type": "error", "message": "boom"},
    ]
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(events))
    resp = await _post(client, {"stream": True,
                                "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    err_frame = frames[-1]
    assert err_frame["choices"][0]["finish_reason"] == "stop"
    assert err_frame["error"]["type"] == "upstream_error"
    assert resp.text.rstrip().endswith("data: [DONE]")


@pytest.mark.anyio
async def test_stream_strict_bool_parsing(client, monkeypatch):
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(_simple_events()))
    missing = await _post(client, {"messages": [{"role": "user", "content": "hi"}]})
    false_value = await _post(client, {"stream": False,
                                      "messages": [{"role": "user", "content": "hi"}]})
    assert missing.status_code == false_value.status_code == 200
    assert missing.json()["object"] == false_value.json()["object"] == "chat.completion"
    for value in ("false", 0, 1, None):
        resp = await _post(client, {"stream": value,
                                    "messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 422


@pytest.mark.anyio
async def test_request_validation_and_nullable_model(client, monkeypatch):
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(_simple_events()))
    for model in (None, ""):
        resp = await _post(client, {"model": model,
                                    "messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 200
    assert (await _post(client, {"messages": []})).status_code == 422
    assert (await _post(client, {"messages": [{"role": "tool", "content": "x"}]})).status_code == 422
    assert (await _post(client, {"max_tokens": "1",
                                "messages": [{"role": "user", "content": "x"}]})).status_code == 422


@pytest.mark.anyio
async def test_invalid_json_and_non_object_body(client):
    headers = {"Authorization": "Bearer sk-test", "Content-Type": "application/json"}
    invalid = await client.post("/v1/chat/completions", content="{", headers=headers)
    array = await client.post("/v1/chat/completions", content="[]", headers=headers)
    assert invalid.status_code == 400
    assert array.status_code == 422


@pytest.mark.anyio
async def test_production_without_any_key_fails_closed(monkeypatch, tmp_path):
    import httpx
    import core.user_store as us
    from app.api.v1.openai_compat import _check_auth
    from app.core.config import settings
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(us, "_DB_PATH", tmp_path / "users.db")
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/v1/models", headers={"Authorization": "Bearer arbitrary"})
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_stream_role_frame_is_first_even_on_immediate_error(client, monkeypatch):
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn([
        {"type": "error", "message": "boom"},
    ]))
    resp = await _post(client, {"stream": True,
                                "messages": [{"role": "user", "content": "hi"}]})
    frames = _parse_sse(resp.text)
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"
    assert frames[-1]["error"]["type"] == "upstream_error"
    assert resp.text.rstrip().endswith("data: [DONE]")


@pytest.mark.anyio
async def test_research_map_stream_emits_markdown_and_svg_attachments(client, monkeypatch, tmp_path):
    import tools.export.report as report
    from core.session_memory import get_memory

    monkeypatch.setattr(report, "_EXPORT_DIR", tmp_path)
    get_memory()._store.clear()

    async def map_turn(user_message, session, progress_cb, attachments=None, regenerate=False):
        session.topic = "测试研究地图"
        session.map_data = {
            "landscape": "测试脉络",
            "clusters": [],
            "timeline": [],
            "graph": {
                "nodes": [
                    {"id": "p1", "title": "Paper 1", "year": 2020,
                     "cluster": 0, "role": "foundational"},
                    {"id": "p2", "title": "Paper 2", "year": 2022,
                     "cluster": 0, "role": "bridge"},
                ],
                "edges": [{"source": "p1", "target": "p2", "type": "cites"}],
            },
        }
        yield {"type": "tool_result", "result": {
            "tool": "research_map", "status": "success", "data": {}}}
        yield {"type": "answer", "content": "地图完成", "is_delta": True}
        yield {"type": "done", "thinking": "", "answer": "地图完成",
               "tool_calls": [], "trace_id": "map", "usage": {
                   "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    monkeypatch.setattr(orch, "chat_turn", map_turn)
    resp = await _post(client, {"stream": True, "user": "map-attachment-test",
                                "messages": [{"role": "user", "content": "生成地图"}]})
    frames = _parse_sse(resp.text)
    attachments = frames[-1]["x_soda"]["attachments"]
    assert {item["mimeType"] for item in attachments} == {"text/markdown", "image/svg+xml"}
    assert {item["fileType"] for item in attachments} == {"text", "image"}
    assert all(item["fileUrl"].startswith("http://testserver/files/") for item in attachments)


@pytest.mark.anyio
async def test_research_map_nonstream_emits_markdown_and_svg_attachments(client, monkeypatch, tmp_path):
    import tools.export.report as report
    from core.session_memory import get_memory

    monkeypatch.setattr(report, "_EXPORT_DIR", tmp_path)
    get_memory()._store.clear()

    async def map_turn(user_message, session, progress_cb, attachments=None, regenerate=False):
        session.topic = "测试研究地图"
        session.map_data = {
            "clusters": [], "timeline": [], "landscape": "",
            "graph": {"nodes": [{"id": "p1", "title": "P1", "year": 2020,
                                  "cluster": 0, "role": "foundational"}],
                      "edges": []},
        }
        yield {"type": "tool_result", "result": {
            "tool": "research_map", "status": "success", "data": {}}}
        yield {"type": "answer", "content": "地图完成", "is_delta": True}
        yield {"type": "done", "thinking": "", "answer": "地图完成",
               "tool_calls": [], "trace_id": "map", "usage": {
                   "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    monkeypatch.setattr(orch, "chat_turn", map_turn)
    resp = await _post(client, {"user": "map-attachment-test-nonstream",
                                "messages": [{"role": "user", "content": "生成地图"}]})
    attachments = resp.json()["x_soda"]["attachments"]
    assert {item["mimeType"] for item in attachments} == {"text/markdown", "image/svg+xml"}


def test_attachment_shape_encodes_and_deduplicates(monkeypatch):
    from starlette.requests import Request
    from app.api.v1.openai_compat import _dedupe_attachments, _shape_attachment
    from app.core.config import settings
    monkeypatch.setattr(settings, "public_base_url", "https://agent.example")
    request = Request({"type": "http", "method": "GET", "path": "/",
                       "headers": [], "scheme": "http", "server": ("local", 80)})
    record = {"fileName": "中文 论文.docx", "fileType": "word",
              "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              "size": 42}
    attachment = _shape_attachment(request, record)
    assert attachment["fileUrl"] == "https://agent.example/files/%E4%B8%AD%E6%96%87%20%E8%AE%BA%E6%96%87.docx"
    assert attachment["fileSize"] == 42
    assert _dedupe_attachments([attachment, dict(attachment)]) == [attachment]


# --- multimodal parsing --------------------------------------------------------

def test_extract_user_content_text_only():
    from app.api.v1.multimodal import extract_user_content
    text, media = extract_user_content({"role": "user", "content": "你好"})
    assert text == "你好" and media == []


def test_extract_user_content_array():
    from app.api.v1.multimodal import extract_user_content
    msg = {"role": "user", "content": [
        {"type": "text", "text": "总结这篇"},
        {"type": "file", "file": {"url": "https://x/doc.pdf", "filename": "doc.pdf"}},
        {"type": "image_url", "image_url": {"url": "https://x/a.png"}},
        {"type": "input_audio", "input_audio": {"url": "https://x/a.mp3", "format": "mp3"}},
    ]}
    text, media = extract_user_content(msg)
    assert text == "总结这篇"
    kinds = [m.type for m in media]
    assert kinds == ["file", "image", "audio"]
    assert media[0].url == "https://x/doc.pdf"
    assert media[2].format == "mp3"


def test_extract_user_content_preserves_bare_file_id_without_using_as_url():
    from app.api.v1.multimodal import extract_user_content
    _text, media = extract_user_content({"role": "user", "content": [
        {"type": "file", "file": {"file_id": "platform-id", "filename": "a.pdf"}},
    ]})
    assert media[0].url == ""
    assert media[0].file_id == "platform-id"


@pytest.mark.anyio
async def test_process_media_parts_rejects_unresolvable_file_id():
    from app.api.v1.multimodal import MediaPart, process_media_parts
    notes, attachments, errors = await process_media_parts([
        MediaPart(type="file", file_id="platform-id", filename="a.pdf")
    ])
    assert notes == [] and attachments == []
    assert errors and "file.url" in errors[0]


@pytest.mark.anyio
async def test_process_media_parts_registers_image_without_vlm(monkeypatch, tmp_path):
    import base64
    import tools.ingest.attachments as attachment_ingest
    from app.api.v1.multimodal import MediaPart, process_media_parts

    monkeypatch.setattr(attachment_ingest, "UPLOAD_DIR", tmp_path)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii")
    notes, attachments, errors = await process_media_parts([
        MediaPart(type="image", url=f"data:image/png;base64,{png}")
    ])
    assert errors == []
    assert len(attachments) == 1
    assert attachments[0]["ext"] == "png"
    assert attachments[0]["multimodal_status"] == "pending"
    assert "按需" in notes[0]


@pytest.mark.anyio
async def test_process_media_parts_degrades_audio():
    from app.api.v1.multimodal import MediaPart, process_media_parts
    notes, attachments, errors = await process_media_parts(
        [MediaPart(type="audio", url="https://example.org/a.mp3", format="mp3")])
    assert attachments == [] and errors == []
    assert notes and "不支持音频解析" in notes[0]


# --- session memory ------------------------------------------------------------

def test_conversation_key_stable_and_distinct():
    from core.session_memory import conversation_key
    msgs = [{"role": "user", "content": "研究 GNN"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "继续"}]
    k1 = conversation_key(msgs)
    k2 = conversation_key(msgs[:1])
    assert k1 == k2  # same conversation, later turn
    k3 = conversation_key([{"role": "user", "content": "另一个主题"}])
    assert k3 != k1


def test_session_memory_ttl_and_lru():
    from core.session_memory import SessionMemory
    mem = SessionMemory(ttl_seconds=-1, max_sessions=3)  # everything expired
    s1, created1 = mem.get_or_create("a")
    assert created1
    s2, created2 = mem.get_or_create("a")  # expired -> recreated
    assert created2

    mem2 = SessionMemory(ttl_seconds=9999, max_sessions=2)
    mem2.get_or_create("a")
    mem2.get_or_create("b")
    mem2.get_or_create("c")  # evicts LRU "a"
    _, created = mem2.get_or_create("a")
    assert created  # was evicted


def test_seed_session_from_messages():
    from agents.session import ChatSession
    from core.session_memory import seed_session_from_messages
    s = ChatSession()
    seed_session_from_messages(s, [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "第一条"},
        {"role": "assistant", "content": "回复一"},
        {"role": "user", "content": "最新问题"},
    ])
    # trailing user message dropped (it's the new turn input)
    assert [m["content"] for m in s.messages] == ["第一条", "回复一"]
