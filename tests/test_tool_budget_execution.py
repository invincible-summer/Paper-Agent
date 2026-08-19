"""execute_tool reads budgets from the runtime store; breaker admin controls."""
from __future__ import annotations

import asyncio

from agents import tools_impl
from agents.session import ChatSession
from core.tool_protocol import ErrorCode, ok
from core.turn_execution import TurnExecutionContext

import core.tool_budget_store as budget_store


def test_execute_tool_uses_runtime_budget(monkeypatch):
    # Injected policy bypasses range validation on purpose: the point is that
    # execute_tool honours the store value for both timeout and stats.
    monkeypatch.setattr(budget_store, "get_tool_budget_policy", lambda: budget_store.ToolBudgetPolicy(
        budgets={"search_papers": 0.2}, default_budget_seconds=30.0, reserve_seconds=8.0))

    async def slow(args, session, progress_cb):
        await asyncio.sleep(1)
        return ok("search_papers", "late")

    monkeypatch.setitem(tools_impl._IMPLS, "search_papers", slow)
    result = asyncio.run(tools_impl.execute_tool(
        {"name": "search_papers", "args": {"topic": "x"}},
        ChatSession(),
        execution_context=TurnExecutionContext.openai_api(soft_timeout_seconds=30, hard_timeout_seconds=40),
    ))
    assert result.error_code == ErrorCode.TIMEOUT
    assert result.stats["configured_timeout_ms"] == 200
    assert result.stats["timeout_kind"] == "tool_budget"


def test_execute_tool_falls_back_to_code_default_budget(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(budget_store, "get_tool_budget_policy", boom)

    async def quick(args, session, progress_cb):
        return ok("search_papers", "done")

    monkeypatch.setitem(tools_impl._IMPLS, "search_papers", quick)
    result = asyncio.run(tools_impl.execute_tool(
        {"name": "search_papers", "args": {"topic": "x"}},
        ChatSession(),
        execution_context=TurnExecutionContext.openai_api(soft_timeout_seconds=60, hard_timeout_seconds=70),
    ))
    assert result.error_code is None
    assert result.stats["configured_timeout_ms"] == 45000


def test_tool_breaker_snapshot_and_force_close():
    from core.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker()
    assert breaker.force_close("never_tracked") is False
    for _ in range(3):
        breaker.record_failure("search_papers")
    snap = breaker.snapshot()["search_papers"]
    assert snap["state"] == "open"
    assert snap["remaining_seconds"] > 0
    assert breaker.force_close("search_papers") is True
    snap = breaker.snapshot()["search_papers"]
    assert snap["state"] == "closed"
    assert snap["consecutive_failures"] == 0
    # Two failures (below threshold) surface as half_open only after an open
    # window elapsed; without a window they read closed with a failure count.
    breaker.record_failure("deep_read")
    breaker.record_failure("deep_read")
    assert breaker.snapshot()["deep_read"]["consecutive_failures"] == 2
