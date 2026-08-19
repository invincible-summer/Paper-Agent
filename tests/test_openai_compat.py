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
    # 2) reasoning frames (thinking + args-aware tool progress)
    reasonings = [f["choices"][0]["delta"].get("reasoning") for f in frames]
    assert "想想" in reasonings
    assert any(r and "正在检索：gnn" in r for r in reasonings)
    # 3) content frames
    contents = [f["choices"][0]["delta"].get("content") for f in frames]
    assert "".join(c or "" for c in contents) == "结果如下"
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
    tool_history = await _post(client, {"messages": [
        {"role": "tool", "content": "ignored external tool history"},
        {"role": "user", "content": "x"},
    ]})
    assert tool_history.status_code == 200
    assert (await _post(client, {"max_tokens": "1",
                                "messages": [{"role": "user", "content": "x"}]})).status_code == 422


@pytest.mark.anyio
async def test_stream_bridges_internal_progress(client, monkeypatch):
    async def fake(user_message, session, progress_cb, attachments=None,
                   regenerate=False, checkpoint_cb=None,
                   execution_context=None, system_instructions=""):
        progress_cb("正在执行谱系语义聚类...")
        yield {"type": "answer", "content": "完成", "is_delta": True}
        yield {"type": "done", "thinking": "", "answer": "完成",
               "tool_calls": [], "trace_id": "p",
               "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                         "total_tokens": 2}}

    monkeypatch.setattr(orch, "chat_turn", fake)
    resp = await _post(client, {"stream": True,
                                "messages": [{"role": "user", "content": "map"}]})
    reasonings = [
        f["choices"][0]["delta"].get("reasoning")
        for f in _parse_sse(resp.text)
    ]
    assert "正在执行谱系语义聚类..." in reasonings


@pytest.mark.anyio
async def test_stream_deadline_closes_protocol_and_cancels_producer(
        client, monkeypatch):
    import app.api.v1.openai_compat as compat

    cancelled = asyncio.Event()

    async def slow(user_message, session, progress_cb, attachments=None,
                   regenerate=False, **kwargs):
        try:
            await asyncio.sleep(10)
            yield {"type": "answer", "content": "late", "is_delta": True}
        finally:
            cancelled.set()

    monkeypatch.setattr(orch, "chat_turn", slow)
    monkeypatch.setattr(compat, "_API_SOFT_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(compat, "_API_HARD_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(compat, "_HEARTBEAT_SECONDS", 0.02)
    resp = await _post(client, {"stream": True,
                                "messages": [{"role": "user", "content": "slow"}]})
    assert resp.status_code == 200
    assert "平台交互时限" in resp.text
    assert resp.text.rstrip().endswith("data: [DONE]")
    frames = _parse_sse(resp.text)
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"
    assert cancelled.is_set()


@pytest.mark.anyio
async def test_stream_coalesces_tiny_provider_deltas(client, monkeypatch):
    events = [
        {"type": "answer", "content": "中", "is_delta": True}
        for _ in range(1500)
    ] + [{
        "type": "done", "thinking": "", "answer": "中" * 1500,
        "tool_calls": [], "trace_id": "batch",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1500,
                  "total_tokens": 1501},
    }]
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(events))
    resp = await _post(client, {"stream": True,
                                "messages": [{"role": "user", "content": "go"}]})
    frames = _parse_sse(resp.text)
    content_frames = [
        f["choices"][0]["delta"].get("content")
        for f in frames if f["choices"][0]["delta"].get("content")
    ]
    assert "".join(content_frames) == "中" * 1500
    assert len(content_frames) <= 10
    assert len(resp.content) < 25_000


@pytest.mark.anyio
async def test_system_instructions_and_session_id_are_supported(client, monkeypatch):
    captured = {}

    async def fake(user_message, session, progress_cb, attachments=None,
                   regenerate=False, checkpoint_cb=None,
                   execution_context=None, system_instructions=""):
        captured["system"] = system_instructions
        yield {"type": "answer", "content": "ok", "is_delta": True}
        yield {"type": "done", "thinking": "", "answer": "ok",
               "tool_calls": [], "trace_id": "sys",
               "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                         "total_tokens": 2}}

    monkeypatch.setattr(orch, "chat_turn", fake)
    resp = await _post(client, {
        "stream": True,
        "sessionId": "qing-session-1",
        "messages": [
            {"role": "system", "content": "请使用简洁中文"},
            {"role": "user", "content": "hi"},
        ],
    })
    assert resp.status_code == 200
    assert captured["system"] == "请使用简洁中文"


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
async def test_research_map_stream_default_emits_svg_attachment(client, monkeypatch, tmp_path):
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
    assert {item["mimeType"] for item in attachments} == {"image/svg+xml"}
    assert {item["fileType"] for item in attachments} == {"image"}
    assert all(item["fileUrl"].startswith("http://testserver/files/") for item in attachments)


@pytest.mark.anyio
async def test_research_map_nonstream_default_emits_svg_attachment(client, monkeypatch, tmp_path):
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
    assert {item["mimeType"] for item in attachments} == {"image/svg+xml"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stream", "changes", "expected_mimes", "has_mermaid"),
    [
        (True, {"research_map_svg_enabled": True,
                "research_map_mermaid_enabled": True,
                "research_map_html_enabled": True,
                "research_map_markdown_enabled": True},
         ["text/markdown", "image/svg+xml", "text/html"], True),
        (False, {"research_map_svg_enabled": False,
                 "research_map_mermaid_enabled": True,
                 "research_map_html_enabled": False,
                 "research_map_markdown_enabled": False}, [], True),
        (True, {"research_map_svg_enabled": False,
                "research_map_mermaid_enabled": False,
                "research_map_html_enabled": True,
                "research_map_markdown_enabled": True},
         ["text/markdown", "text/html"], False),
    ],
)
async def test_research_map_combination_protocol(
    client, monkeypatch, tmp_path, stream, changes, expected_mimes, has_mermaid,
):
    from core.session_memory import get_memory

    get_memory()._store.clear()
    _set_display_policy(tmp_path, **changes)

    async def map_turn(user_message, session, progress_cb, attachments=None, regenerate=False):
        session.topic = "策略地图"
        session.map_data = {
            "clusters": [{"id": 0, "label": "主题"}], "timeline": [], "landscape": "",
            "graph": {"nodes": [
                {"id": "p1", "title": "P1", "year": 2020, "cluster": 0,
                 "citation_count": 10, "role": "foundational", "layer": "core"},
                {"id": "p2", "title": "P2", "year": 2022, "cluster": 0,
                 "citation_count": 1, "role": "", "layer": "core"},
            ], "edges": [{"source": "p2", "target": "p1", "type": "cites"}]},
        }
        yield {"type": "tool_result", "result": {
            "tool": "research_map", "status": "success", "data": {}}}
        yield {"type": "answer", "content": "地图完成", "is_delta": True}
        yield {"type": "done", "thinking": "", "answer": "地图完成",
               "tool_calls": [], "trace_id": "map-combination", "usage": {
                   "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    monkeypatch.setattr(orch, "chat_turn", map_turn)
    resp = await _post(client, {"stream": stream, "user": f"map-combo-{stream}-{has_mermaid}",
                                "messages": [{"role": "user", "content": "生成地图"}]})
    if stream:
        assert resp.text.rstrip().endswith("data: [DONE]")
        frames = _parse_sse(resp.text)
        assert sum(frame["choices"][0]["finish_reason"] == "stop" for frame in frames) == 1
        payload, content = frames[-1], _content_of(frames)
    else:
        payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
    assert ("```mermaid" in content) is has_mermaid
    assert content.endswith("地图完成")
    attachments = payload.get("x_soda", {}).get("attachments", [])
    assert [item["mimeType"] for item in attachments] == expected_mimes
    assert all(item["mimeType"] != "text/x-mermaid" for item in attachments)
    for item in attachments:
        expected_type = "image" if item["mimeType"] == "image/svg+xml" else "text"
        assert item["fileType"] == expected_type
        downloaded = await client.get(item["fileUrl"])
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].split(";", 1)[0] == item["mimeType"]
        if item["mimeType"] == "image/svg+xml":
            assert 'preserveAspectRatio="xMidYMid meet"' in downloaded.text
        if item["mimeType"] == "text/markdown":
            assert "## 引用与语义关系" in downloaded.text
        if item["mimeType"] == "text/html":
            assert "attachment" in downloaded.headers["content-disposition"].lower()
            assert "Content-Security-Policy" in downloaded.text


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
async def test_process_media_parts_degrades_unresolvable_file_id_without_network(monkeypatch):
    import tools.ingest.downloader as downloader
    from app.api.v1.multimodal import MediaPart, process_media_parts

    async def unexpected(*args, **kwargs):
        raise AssertionError("bare file_id must not trigger a network request")

    monkeypatch.setattr(downloader, "download_to_temp", unexpected)
    monkeypatch.setattr(downloader, "download_bytes", unexpected)
    notes, attachments, errors = await process_media_parts([
        MediaPart(type="file", file_id="platform-id", filename="a.pdf")
    ])
    assert attachments == [] and errors == []
    assert notes and "缺少可下载的 file.url" in notes[0]
    assert "不会把 file_id 当作路径或网址" in notes[0]


@pytest.mark.anyio
async def test_process_media_parts_prefers_url_when_file_id_is_also_present(monkeypatch, tmp_path):
    import time
    import tools.ingest.downloader as downloader
    from app.api.v1.multimodal import MediaPart, process_media_parts
    from core.api_storage_store import ApiStorageStore
    from core.storage_context import StorageContext

    context = StorageContext.openai_api(root_dir=tmp_path / "openai-api")
    store = ApiStorageStore(context)
    store.initialize()
    current = store.get_policy()
    store.update_policy(
        {"max_upload_bytes": 3 * 1024 * 1024},
        expected_version=current.version,
        updated_by="test",
    )
    now = time.time()
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO api_sessions(id, credential_id, rag_session_id, created_at, "
            "updated_at, last_accessed_at, expires_at) VALUES (?, 'key', ?, ?, ?, ?, ?)",
            ("session-url", "session-url", now, now, now, now + 3600),
        )
        conn.commit()

    seen = {}
    async def fake_download(url, *, temp_dir=None, max_bytes=None, max_redirects=5):
        seen.update(url=url, max_bytes=max_bytes)
        path = Path(temp_dir) / "download.tmp"
        path.write_bytes(b"downloaded through url")
        return path, "text/plain"

    monkeypatch.setattr(downloader, "download_to_temp", fake_download)
    notes, attachments, errors = await process_media_parts(
        [MediaPart(
            type="file", file_id="platform-id",
            url="https://files.example/%E7%AC%94%E8%AE%B0.txt?signature=secret",
        )],
        storage_context=context,
        session_id="session-url",
    )
    assert errors == [] and notes
    assert seen == {
        "url": "https://files.example/%E7%AC%94%E8%AE%B0.txt?signature=secret",
        "max_bytes": 3 * 1024 * 1024,
    }
    assert attachments[0]["filename"] == "笔记.txt"
    assert attachments[0]["source_file_id"] == "platform-id"
    assert attachments[0]["char_count"] > 0


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


# --- markdown card emulation + skill_loaded + display policy ---------------------


def _card_events():
    return [
        {"type": "tool_start", "name": "search_papers", "args": {"topic": "gnn"}},
        {"type": "tool_result", "result": {
            "tool": "search_papers", "status": "success",
            "papers": [{"title": "Paper A", "year": 2021, "citation_count": 5,
                        "fulltext_status": "available"}],
            "candidates": [], "fulltext_core_available": 1,
            "fulltext_core_target": 8, "summary": "检索完成"}},
        {"type": "tool_result", "result": {
            "tool": "integrity_sweep", "status": "success",
            "summary": "可靠性质检完成（2 篇）：无异常 2"}},
        {"type": "answer", "content": "这是最终回答。", "is_delta": True},
        {"type": "done", "thinking": "", "answer": "这是最终回答。",
         "tool_calls": [], "trace_id": "t", "usage": {
             "prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}},
    ]


def _content_of(frames):
    return "".join(f["choices"][0]["delta"].get("content") or "" for f in frames)


@pytest.mark.anyio
async def test_stream_card_before_answer_with_separator(client, monkeypatch):
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(_card_events()))
    resp = await _post(client, {"stream": True, "max_tokens": 500,
                                "messages": [{"role": "user", "content": "搜"}]})
    frames = _parse_sse(resp.text)
    content = _content_of(frames)
    # one-line status card + the mandated search table, then the next tool's
    # one-liner separated by ---, all before the answer
    assert "**🔎 文献检索 · 核心集 1 篇 / 候选 0 篇**" in content
    assert "| 分层 | 标题 | 年份 | 被引 | 全文 | 链接 |" in content
    assert "| 核心 | Paper A | 2021 | 5 | 🟢 | — |" in content
    assert "**🛡️ 可靠性质检** · 可靠性质检完成（2 篇）：无异常 2" in content
    assert "\n---\n\n" in content
    assert content.index("文献检索 · 核心集") < content.index("这是最终回答。")


@pytest.mark.anyio
async def test_nonstream_card_prepended_to_answer(client, monkeypatch):
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(_card_events()))
    resp = await _post(client, {"messages": [{"role": "user", "content": "搜"}]})
    content = resp.json()["choices"][0]["message"]["content"]
    assert content.index("🔎 文献检索") < content.index("这是最终回答。")
    assert "| 分层 | 标题 | 年份 | 被引 | 全文 | 链接 |" in content
    assert content.rstrip().endswith("这是最终回答。")


@pytest.mark.anyio
async def test_skill_loaded_dual_channel(client, monkeypatch):
    events = [
        {"type": "tool_start", "name": "use_skill", "args": {"name": "research_gap"}},
        {"type": "skill_loaded", "name": "research_gap"},
        {"type": "tool_result", "result": {"tool": "use_skill", "status": "success",
                                           "instructions": "内部指令"}},
        {"type": "answer", "content": "按技能流程完成。", "is_delta": True},
        {"type": "done", "thinking": "", "answer": "按技能流程完成。",
         "tool_calls": [], "trace_id": "t", "usage": {
             "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(events))
    resp = await _post(client, {"stream": True,
                                "messages": [{"role": "user", "content": "找空白"}]})
    text = resp.text
    assert "use_skill" not in text and "内部指令" not in text
    frames = _parse_sse(text)
    reasonings = [f["choices"][0]["delta"].get("reasoning") for f in frames]
    assert any(r and "已加载技能《研究空白识别与选题评估》" in r for r in reasonings)
    content = _content_of(frames)
    assert "━━ 📘 技能 · 研究空白识别与选题评估 ━━" in content
    # the in-content line is part of the echoed content (alias-chain invariant)
    assert content.index("📘 技能") < content.index("按技能流程完成。")


def _set_display_policy(tmp_root, **changes):
    from core.api_storage_store import ApiStorageStore
    from core.storage_context import StorageContext

    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_root / "openai-api"))
    store.initialize()
    version = store.get_display_policy().version
    return store.update_display_policy(changes, expected_version=version,
                                       updated_by="test")


@pytest.mark.anyio
async def test_tool_cards_disabled_keeps_search_table(client, monkeypatch, tmp_path):
    _set_display_policy(tmp_path, tool_cards_enabled=False)
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(_card_events()))
    resp = await _post(client, {"stream": True, "max_tokens": 500,
                                "messages": [{"role": "user", "content": "搜"}]})
    frames = _parse_sse(resp.text)
    content = _content_of(frames)
    # status lines are off, but the mandated search-results table stays on
    assert "文献检索" not in content and "可靠性质检" not in content
    assert "| 分层 | 标题 | 年份 | 被引 | 全文 | 链接 |" in content
    assert "这是最终回答。" in content
    # progress lines in the thinking fold remain
    reasonings = [f["choices"][0]["delta"].get("reasoning") for f in frames]
    assert any(r and "正在检索：gnn" in r for r in reasonings)


@pytest.mark.anyio
async def test_small_budget_skips_search_table(client, monkeypatch):
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(_card_events()))
    resp = await _post(client, {"stream": True, "max_tokens": 50,
                                "messages": [{"role": "user", "content": "搜"}]})
    content = _content_of(_parse_sse(resp.text))
    # the full table is skipped under a tight budget so it cannot crowd out
    # the answer; the cheap one-line status cards still appear
    assert "| 分层 |" not in content
    assert "这是最终回答。" in content


@pytest.mark.anyio
async def test_bibtex_and_census_attachments(client, monkeypatch):
    events = [
        {"type": "tool_result", "result": {
            "tool": "citation_export", "status": "success",
            "citations": "@article{a,\n  title={T},\n}", "format": "bibtex",
            "count": 1}},
        {"type": "tool_result", "result": {
            "tool": "field_census", "status": "success",
            "yearly": [{"key": 2023, "name": "2023", "count": 900},
                       {"key": 2024, "name": "2024", "count": 1200}],
            "top_authors": [{"name": "A", "count": 3}],
            "top_institutions": [], "top_venues": []}},
        {"type": "answer", "content": "完成。", "is_delta": True},
        {"type": "done", "thinking": "", "answer": "完成。", "tool_calls": [],
         "trace_id": "t", "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                     "total_tokens": 2}},
    ]
    monkeypatch.setattr(orch, "chat_turn", _stub_chat_turn(events))
    resp = await _post(client, {"stream": True, "user": "extra-attach-test",
                                "messages": [{"role": "user", "content": "导出"}]})
    frames = _parse_sse(resp.text)
    attachments = frames[-1]["x_soda"]["attachments"]
    by_mime = {a["mimeType"]: a for a in attachments}
    assert by_mime["text/plain"]["fileName"].endswith(".bib")
    assert by_mime["image/svg+xml"]["fileName"].endswith(".svg")
    # image attachments carry previewUrl
    assert by_mime["image/svg+xml"]["previewUrl"] == by_mime["image/svg+xml"]["fileUrl"]
    # the census card mentions the attachment
    content = _content_of(frames)
    assert "📑 参考文献导出 · BibTeX · 1 篇" in content
    assert "📊 领域普查" in content


@pytest.mark.anyio
async def test_element_crop_attachment_scope_guard(client, monkeypatch, tmp_path):
    import base64

    from core.config import get_settings
    from core.models import Paper

    settings = get_settings()
    assets = tmp_path / "assets"
    pid = "10.1/xx-test-paper"
    crop_dir = assets / "10.1_xx-test-paper"
    crop_dir.mkdir(parents=True)
    crop_dir.joinpath("figure_1.png").write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="))
    monkeypatch.setattr(settings.reader, "assets_dir", str(assets))

    def result_event(with_paper: bool):
        return {"type": "tool_result", "result": {
            "tool": "explain_element", "status": "success",
            "element": {"element_id": f"{pid}::figure::1", "paper_id": pid,
                        "kind": "figure", "page": 2, "caption": "Fig 1",
                        "asset_url": f"/api/v1/elements/assets/{pid}/figure_1.png"}}}

    async def owned(user_message, session, progress_cb, attachments=None, regenerate=False):
        session.papers.append(Paper(id=pid, title="Owner Paper"))
        yield result_event(True)
        yield {"type": "answer", "content": "解读完成。", "is_delta": True}
        yield {"type": "done", "thinking": "", "answer": "解读完成。",
               "tool_calls": [], "trace_id": "t", "usage": {
                   "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    async def foreign(user_message, session, progress_cb, attachments=None, regenerate=False):
        # paper NOT in this session's scope: crop must not be attached
        yield result_event(True)
        yield {"type": "answer", "content": "解读完成。", "is_delta": True}
        yield {"type": "done", "thinking": "", "answer": "解读完成。",
               "tool_calls": [], "trace_id": "t", "usage": {
                   "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    monkeypatch.setattr(orch, "chat_turn", owned)
    resp = await _post(client, {"stream": True, "user": "crop-owned",
                                "messages": [{"role": "user", "content": "解读图"}]})
    frames = _parse_sse(resp.text)
    attachments = frames[-1]["x_soda"]["attachments"]
    assert [a for a in attachments if a["mimeType"] == "image/png"], attachments

    monkeypatch.setattr(orch, "chat_turn", foreign)
    resp = await _post(client, {"stream": True, "user": "crop-foreign",
                                "messages": [{"role": "user", "content": "解读图"}]})
    frames = _parse_sse(resp.text)
    assert "x_soda" not in frames[-1] or not [
        a for a in frames[-1].get("x_soda", {}).get("attachments", [])
        if a["mimeType"] == "image/png"]


@pytest.mark.anyio
async def test_stream_role_frame_precedes_slow_turn_preparation(monkeypatch):
    import app.api.v1.openai_compat as compat

    preparation_started = asyncio.Event()
    preparation_cancelled = asyncio.Event()

    async def slow_prepare(body, principal):
        preparation_started.set()
        try:
            await asyncio.sleep(10)
        finally:
            preparation_cancelled.set()

    monkeypatch.setattr(compat, "_prepare_turn", slow_prepare)
    monkeypatch.setenv("AGENT_API_KEY", "")
    app = create_app()
    request_body = json.dumps({
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    sent: asyncio.Queue = asyncio.Queue()
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        await sent.put(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"},
        "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions", "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer sk-test"),
            (b"content-type", b"application/json"),
            (b"host", b"testserver"),
        ],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        start = await asyncio.wait_for(sent.get(), timeout=0.2)
        first_body = await asyncio.wait_for(sent.get(), timeout=0.2)
        assert start["type"] == "http.response.start"
        assert first_body["type"] == "http.response.body"
        assert b'"role": "assistant"' in first_body["body"]
        assert first_body.get("more_body") is True
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert preparation_started.is_set()
    assert preparation_cancelled.is_set()

@pytest.mark.anyio
async def test_mermaid_budget_omits_whole_block_and_keeps_answer(client, monkeypatch, tmp_path):
    from core.session_memory import get_memory

    get_memory()._store.clear()
    _set_display_policy(tmp_path, research_map_mermaid_enabled=True)

    async def map_turn(user_message, session, progress_cb, attachments=None, regenerate=False):
        session.topic = "预算地图"
        session.map_data = {
            "clusters": [{"id": 0, "label": "主题"}],
            "graph": {"nodes": [
                {"id": f"p{i}", "title": "很长的论文标题" * 5, "year": 2000 + i,
                 "cluster": 0, "citation_count": i, "layer": "core"}
                for i in range(8)
            ], "edges": [{"source": f"p{i}", "target": f"p{i - 1}", "type": "cites"}
                         for i in range(1, 8)]},
        }
        yield {"type": "tool_result", "result": {
            "tool": "research_map", "status": "success", "data": {}}}
        yield {"type": "answer", "content": "最终回答", "is_delta": True}
        yield {"type": "done", "thinking": "", "answer": "最终回答",
               "tool_calls": [], "trace_id": "map-budget", "usage": {}}

    monkeypatch.setattr(orch, "chat_turn", map_turn)
    resp = await _post(client, {"stream": True, "max_tokens": 30, "user": "map-budget",
                                "messages": [{"role": "user", "content": "生成地图"}]})
    content = _content_of(_parse_sse(resp.text))
    assert "```mermaid" not in content
    assert "Mermaid 正文因 max_tokens 预算不足已整块省略" in content
    assert content.endswith("最终回答")
