from __future__ import annotations

from types import SimpleNamespace

import pytest

import agents.orchestrator as orchestrator
from agents.session import ChatSession
from core.tool_budget_store import ToolBudgetPolicy
from core.tool_protocol import ErrorCode, ok
from core.turn_execution import TurnExecutionContext


def _policy(**budgets):
    return ToolBudgetPolicy(
        budgets=budgets, default_budget_seconds=30, reserve_seconds=8,
        api_turn_soft_seconds=95,
    )


def test_admission_first_tool_can_be_clipped_but_followup_needs_full_budget():
    context = TurnExecutionContext.openai_api(
        soft_timeout_seconds=24, tool_budget_policy=_policy(search_papers=45, ask_papers=10))
    first = context.admit_public_tool("search_papers")
    assert first.allowed
    assert 15 <= first.effective_timeout_seconds <= 16.1
    context.mark_public_tool_started()
    context.started_at -= 7
    followup = context.admit_public_tool("ask_papers")
    assert not followup.allowed
    assert "完整管理员预算" in followup.reason


def test_admission_hard_cap_and_timeout_closure():
    context = TurnExecutionContext.openai_api(tool_budget_policy=_policy())
    for _ in range(3):
        assert context.admit_public_tool("ask_papers").allowed
        context.mark_public_tool_started()
    assert not context.admit_public_tool("ask_papers").allowed
    context.close_public_tools("partial timeout")
    denied = context.admit_public_tool("check_structure")
    assert not denied.allowed and "partial timeout" in denied.reason


class _BatchThenAnswer:
    model = "api-gate-test"

    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield SimpleNamespace(
                content="", additional_kwargs={}, usage_metadata=None,
                tool_call_chunks=[
                    {"index": 0, "name": "ask_papers", "args": '{"query":"a"}'},
                    {"index": 1, "name": "check_structure", "args": '{}'},
                ],
            )
        else:
            yield SimpleNamespace(content="已总结", additional_kwargs={}, usage_metadata=None,
                                  tool_call_chunks=[])


@pytest.mark.anyio
async def test_api_one_public_tool_per_model_batch(monkeypatch):
    monkeypatch.setattr(orchestrator, "get_llm", lambda tier="light": _BatchThenAnswer())
    executed: list[str] = []

    async def execute(tool_call, session, progress_cb, **kwargs):
        executed.append(tool_call["name"])
        return ok(tool_call["name"], "ok")

    monkeypatch.setattr(orchestrator, "execute_tool", execute)
    context = TurnExecutionContext.openai_api(tool_budget_policy=_policy())
    events = [event async for event in orchestrator.chat_turn(
        "batch", ChatSession(channel="openai_api"), execution_context=context)]
    results = [e["result"] for e in events if e.get("type") == "tool_result"]
    assert executed == ["ask_papers"]
    assert len(results) == 2
    assert results[1]["error"]["code"] == ErrorCode.TIMEOUT
    assert context.stop_public_tools
