"""Search budget regressions after network full-text retirement."""
from __future__ import annotations

import asyncio

from agents import search_agent as sa
from core.models import Paper
import core.paper_search_settings_store as paper_store
import core.tool_budget_store as budget_store


def _score(paper):
    paper.relevance_score = 0.5
    return paper


def _run(monkeypatch, budget=20.0):
    class StubManager:
        instances = []
        def __init__(self, **kwargs):
            self.kwargs = kwargs; self.instances.append(self)
        async def search_all(self, queries, progress_callback=None, **kwargs):
            return [Paper(id="p1", title="Paper", source="arxiv", abstract="摘要")]
    monkeypatch.setattr(sa, "SearchManager", StubManager)
    monkeypatch.setattr(budget_store, "get_tool_budget", lambda name: budget)
    monkeypatch.setattr(paper_store, "get_paper_search_policy", lambda: paper_store.PaperSearchPolicy(sources={"arxiv": True}))
    monkeypatch.setattr(sa, "rerank_papers", lambda papers, query_text, embed_fn=None: [_score(p) for p in papers])
    monkeypatch.setattr(sa, "_embed_ok", lambda _q: False)
    monkeypatch.setattr(sa, "_persist_and_index_results", lambda *args, **kwargs: None)
    monkeypatch.setattr(sa, "_understand", lambda topic, conception, language: asyncio.sleep(0, result=sa._fallback_plan(topic)))
    state = {"topic": "graph structure learning", "language": "both", "user_conception": ""}
    return asyncio.run(sa.search_agent(state)), StubManager


def test_search_budget_has_no_remote_probe_stage(monkeypatch):
    state, manager = _run(monkeypatch)
    assert len(state["papers"]) + len(state["candidates"]) == 1
    assert "fulltext_probe" not in state.get("completed_stages", [])
    assert "fulltext_probe" not in state.get("skipped_stages", [])
    assert "fulltext_statuses" not in state
    assert manager.instances[0].kwargs["search_deadline_seconds"] <= 20


def test_search_timeout_still_returns_metadata_snapshot(monkeypatch):
    state, _ = _run(monkeypatch, budget=2.0)
    assert state["current_phase"] == "search_done"
    assert state["papers"] or state["candidates"]
    assert all("fulltext_status" not in p.to_dict()
               for p in state["papers"] + state["candidates"])
