"""End-to-end ASGI coverage for /v1 structured Checkpoint recovery."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import agents.orchestrator as orchestrator
from app.main import create_app
from core.api_checkpoint import _STORE_CACHE


@pytest.mark.anyio
async def test_openai_turn_recovers_structured_state_after_store_restart(monkeypatch, tmp_path: Path):
    import httpx

    root = tmp_path / "openai-api"
    monkeypatch.setenv("OPENAI_API_STORAGE_ROOT", str(root))
    monkeypatch.setenv("AGENT_API_KEY", "")
    _STORE_CACHE.clear()
    seen: list[tuple[str, str, str]] = []

    async def fake_turn(
        user_message, session, progress_cb, attachments=None, regenerate=False,
        checkpoint_cb=None,
    ):
        seen.append((user_message, session.session_id, session.topic))
        if user_message == "建立研究":
            session.topic = "持久化主题"
            session.map_data = {"clusters": [{"label": "理论"}]}
        answer = "状态已建立" if user_message == "建立研究" else "状态已恢复"
        yield {"type": "answer", "content": answer, "is_delta": True}
        yield {"type": "done", "thinking": "DO_NOT_STORE_REASONING", "answer": answer,
               "tool_calls": [], "trace_id": "trace-sensitive",
               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    monkeypatch.setattr(orchestrator, "chat_turn", fake_turn)
    transport = httpx.ASGITransport(app=create_app())
    headers = {"Authorization": "Bearer local-test-key"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/v1/chat/completions", headers=headers, json={
            "user": "platform-user",
            "messages": [{"role": "user", "content": "建立研究"}],
        })
        assert first.status_code == 200
        assert first.json()["choices"][0]["message"]["content"] == "状态已建立"

        # Simulates a new process: no in-memory store/session object survives.
        _STORE_CACHE.clear()
        second = await client.post("/v1/chat/completions", headers=headers, json={
            "user": "platform-user",
            "messages": [
                {"role": "user", "content": "建立研究"},
                {"role": "assistant", "content": "状态已建立"},
                {"role": "user", "content": "继续"},
            ],
        })
    assert second.status_code == 200
    assert second.json()["choices"][0]["message"]["content"] == "状态已恢复"
    assert seen[1][1] == seen[0][1]
    assert seen[1][2] == "持久化主题"

    db_bytes = (root / "state.db").read_bytes()
    assert b"DO_NOT_STORE_REASONING" not in db_bytes
    assert b"trace-sensitive" not in db_bytes
    assert b"local-test-key" not in db_bytes
    with __import__("sqlite3").connect(root / "state.db") as conn:
        blob = conn.execute("SELECT checkpoint_blob FROM api_sessions LIMIT 1").fetchone()[0]
    decoded = __import__("zlib").decompress(blob).decode("utf-8")
    assert '"messages"' not in decoded
    assert '"map_data"' in decoded


@pytest.mark.anyio
async def test_openai_error_turn_creates_no_final_alias(monkeypatch, tmp_path: Path):
    import httpx
    import sqlite3

    root = tmp_path / "openai-api"
    monkeypatch.setenv("OPENAI_API_STORAGE_ROOT", str(root))
    monkeypatch.setenv("AGENT_API_KEY", "")
    _STORE_CACHE.clear()

    async def failed_turn(user_message, session, progress_cb, attachments=None,
                          regenerate=False, checkpoint_cb=None):
        session.topic = "partial"
        if checkpoint_cb:
            await checkpoint_cb(session)
        yield {"type": "error", "message": "upstream failed"}

    monkeypatch.setattr(orchestrator, "chat_turn", failed_turn)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer local-test-key"},
            json={"messages": [{"role": "user", "content": "会中断"}]},
        )
    assert response.status_code == 500
    with sqlite3.connect(root / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_session_aliases").fetchone()[0] == 0
        # A partial structured checkpoint is allowed and aids fault recovery,
        # but it is intentionally unreachable by a final conversation alias.
        assert conn.execute("SELECT COUNT(*) FROM api_sessions WHERE checkpoint_blob IS NOT NULL").fetchone()[0] == 1
