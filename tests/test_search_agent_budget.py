"""Search-pipeline stage budgets must share the search_papers tool budget.

Regression for the production tool_timeout: retrieval (30 s) + full-text
probing (30 s) + understand-LLM used to stack independently and overflow the
outer 45 s budget, voiding already-retrieved results.
"""
from __future__ import annotations

import asyncio

from agents import search_agent as sa
from core.models import Paper
import core.paper_search_settings_store as paper_store
import core.tool_budget_store as budget_store


def make_paper() -> Paper:
    return Paper(id="p1", title="graph structure learning survey", source="arxiv")


class StubManager:
    delay = 0.0
    instances: list["StubManager"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        StubManager.instances.append(self)

    async def search_all(self, queries, progress_callback=None, **kwargs):
        if StubManager.delay:
            await asyncio.sleep(StubManager.delay)
        return [make_paper()]


def _prepare(monkeypatch, *, budget: float, search_delay: float = 0.0):
    StubManager.delay = search_delay
    StubManager.instances.clear()
    monkeypatch.setattr(sa, "SearchManager", StubManager)
    monkeypatch.setattr(budget_store, "get_tool_budget", lambda name: budget)
    monkeypatch.setattr(
        paper_store, "get_paper_search_policy",
        lambda: paper_store.PaperSearchPolicy(sources={"arxiv": True}))
    # Keep CPU/embedding stages offline and instant.
    monkeypatch.setattr(
        sa, "rerank_papers",
        lambda papers, query_text, embed_fn=None: [_score(p) for p in papers])
    monkeypatch.setattr(sa, "_embed_ok", lambda query_text: False)
    monkeypatch.setattr(sa, "_persist_and_index_results", lambda *args, **kwargs: None)
    async def instant_understand(topic, conception, language):
        return sa._fallback_plan(topic)
    monkeypatch.setattr(sa, "_understand", instant_understand)


def _score(p: Paper) -> Paper:
    p.relevance_score = 0.9
    return p


def _run(state=None):
    return asyncio.run(sa.search_agent(state or {"topic": "graph structure learning"}))


def test_understand_timeout_falls_back_to_raw_topic(monkeypatch):
    _prepare(monkeypatch, budget=45.0)
    monkeypatch.setattr(sa, "_UNDERSTAND_TIMEOUT_SECONDS", 0.05)

    async def stalled(topic, conception, language):
        await asyncio.sleep(5)

    monkeypatch.setattr(sa, "_understand", stalled)
    state = _run({"topic": "graph structure learning"})
    assert state["search_queries"] == ["graph structure learning"]
    assert state["research_goal"] == "graph structure learning"
    assert len(state["papers"]) + len(state["candidates"]) == 1


def test_search_deadline_is_clamped_to_remaining_budget(monkeypatch):
    _prepare(monkeypatch, budget=20.0)
    _run()
    deadline = StubManager.instances[0].kwargs["search_deadline_seconds"]
    # min(policy 30, remaining(≈20) - 8) ≈ 12
    assert 11.0 <= deadline <= 12.5


def test_probe_is_skipped_when_remaining_budget_is_low(monkeypatch):
    _prepare(monkeypatch, budget=2.0, search_delay=1.0)
    called = []

    async def fake_verify(papers, storage_context=None, progress_callback=None,
                          timeout_seconds=None):
        called.append(timeout_seconds)
        return {}

    monkeypatch.setattr("tools.pdf.availability.verify_papers_fulltext", fake_verify)
    state = _run({"topic": "graph structure learning"})
    assert called == []
    assert state["fulltext_statuses"] == {}


def test_probe_budget_is_clamped_to_remaining_time(monkeypatch):
    _prepare(monkeypatch, budget=6.5, search_delay=1.0)
    captured = {}

    async def fake_verify(papers, storage_context=None, progress_callback=None,
                          timeout_seconds=None):
        captured["timeout"] = timeout_seconds
        return {p.id: "available" for p in papers}

    monkeypatch.setattr("tools.pdf.availability.verify_papers_fulltext", fake_verify)
    state = _run({"topic": "graph structure learning"})
    # min(policy 30, remaining(≈5.5) - 3) ≈ 2.5 — never the full 30 s again.
    assert 2.0 <= captured["timeout"] <= 3.0
    assert state["fulltext_statuses"]["p1"] == "available"
