from __future__ import annotations

import asyncio

import pytest

from agents.session import ChatSession
from agents import tools_impl
from core.models import Paper
from core.tool_budget_store import ToolBudgetPolicy
from core.turn_execution import TurnExecutionContext


@pytest.mark.anyio
async def test_outer_search_timeout_returns_published_snapshot(monkeypatch):
    async def fake_search(state, progress_callback=None, invocation_context=None):
        state.update({
            "papers": [Paper(id="p1", title="Recovered paper")],
            "candidates": [Paper(id="p2", title="Candidate")],
            "sub_directions": [{"name": "theory"}],
            "search_queries": ["theory query"],
            "completed_stages": ["intent", "retrieval", "fast_rerank", "tiering"],
            "skipped_stages": ["fulltext_probe", "persistence"],
        })
        state["snapshot_callback"](state)
        await asyncio.sleep(1)
        return state

    monkeypatch.setattr("agents.search_agent.search_agent", fake_search)
    policy = ToolBudgetPolicy(
        budgets={"search_papers": 0.05}, default_budget_seconds=0.05,
        reserve_seconds=0, api_turn_soft_seconds=30,
    )
    context = TurnExecutionContext.openai_api(
        soft_timeout_seconds=2, tool_budget_policy=policy)
    session = ChatSession(channel="openai_api")
    result = await tools_impl.execute_tool(
        {"name": "search_papers", "args": {"topic": "recover"}},
        session, execution_context=context,
    )
    assert result.status == "partial"
    assert result.data["budget_exhausted"] is True
    assert result.data["papers"][0]["id"] == "p1"
    assert "tiering" in result.data["completed_stages"]
    assert session.papers[0].id == "p1"
    assert result.stats["timeout_kind"] == "tool_budget"
