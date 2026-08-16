"""Orchestrator Checkpoint callback boundaries for API sessions."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agents.orchestrator as orchestrator
from agents.session import ChatSession
from core.tool_protocol import ok


class _ToolThenAnswerLLM:
    model = "checkpoint-test"

    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield SimpleNamespace(
                content="", additional_kwargs={}, usage_metadata=None,
                tool_call_chunks=[{
                    "index": 0, "name": "search_papers",
                    "args": '{"topic":"checkpoint"}',
                }],
            )
        else:
            yield SimpleNamespace(
                content="最终回答", additional_kwargs={}, usage_metadata=None,
                tool_call_chunks=[],
            )


@pytest.mark.anyio
async def test_successful_tool_batch_and_done_invoke_checkpoint(monkeypatch):
    monkeypatch.setattr(orchestrator, "get_llm", lambda tier="light": _ToolThenAnswerLLM())

    async def execute(tool_call, session, progress_cb):
        session.topic = "工具已更新的主题"
        return ok("search_papers", "done", papers=[])

    monkeypatch.setattr(orchestrator, "execute_tool", execute)
    callbacks: list[tuple[bool, str | None, str]] = []

    async def checkpoint(session, *, final=False, final_answer=None):
        callbacks.append((final, final_answer, session.topic))

    session = ChatSession(
        channel="openai_api", session_id="api-checkpoint",
    )
    events = [event async for event in orchestrator.chat_turn(
        "研究", session, checkpoint_cb=checkpoint
    )]
    assert any(event["type"] == "done" for event in events)
    assert callbacks[0] == (False, None, "工具已更新的主题")
    assert callbacks[-1] == (True, "最终回答", "工具已更新的主题")


@pytest.mark.anyio
async def test_attachment_registration_invokes_partial_checkpoint(monkeypatch):
    class _AnswerLLM:
        model = "checkpoint-test"
        def bind_tools(self, tools, **kwargs):
            return self
        async def astream(self, messages, **kwargs):
            yield SimpleNamespace(content="完成", additional_kwargs={}, usage_metadata=None,
                                  tool_call_chunks=[])

    monkeypatch.setattr(orchestrator, "get_llm", lambda tier="light": _AnswerLLM())
    monkeypatch.setattr(orchestrator, "_index_attachments", lambda session, attachments: None)
    calls: list[tuple[bool, int]] = []

    async def checkpoint(session, *, final=False, final_answer=None):
        calls.append((final, len(session.attachments)))

    session = ChatSession(channel="openai_api", session_id="api-attachment")
    _ = [event async for event in orchestrator.chat_turn(
        "看看文件", session,
        attachments=[{"id": "a1", "filename": "a.pdf", "char_count": 10}],
        checkpoint_cb=checkpoint,
    )]
    assert calls[0] == (False, 1)
    assert calls[-1] == (True, 1)
