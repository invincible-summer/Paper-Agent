"""Tests for search dedup + ranking determinism (DESIGN D-006/D-046/D-067).

The dedup pipeline and base helpers are pure functions over Paper objects, so
they are safe to unit-test without network access.
"""

from core.models import Paper
from tools.search.base import normalize_title, title_similarity
from tools.search.manager import SearchManager


def _paper(title, doi=None, source="openalex", citations=0, year=2023):
    return Paper(
        id=f"doi:{doi}" if doi else f"hash:{title}",
        title=title,
        authors=["Author A"],
        year=year,
        doi=doi,
        source=source,
        citation_count=citations,
    )


def test_normalize_title_lowercases_and_strips_punctuation():
    assert normalize_title("  Hello, World!!  ") == "hello world"


def test_title_similarity_identical_titles():
    assert title_similarity("Machine Learning", "machine learning") == 1.0


def test_title_similarity_disjoint():
    assert title_similarity("alpha", "beta") == 0.0


def test_dedup_merges_by_doi():
    mgr = SearchManager()
    a = _paper("Title A", doi="10.1/abc", source="openalex", citations=5)
    b = _paper("Duplicate Title A", doi="10.1/abc", source="arxiv", citations=10)
    out = mgr._dedup([a, b])
    assert len(out) == 1
    # merge should have kept the duplicate's higher citation count
    assert out[0].citation_count >= 5


def test_dedup_merges_by_fuzzy_title():
    mgr = SearchManager()
    a = _paper("Deep Reinforcement Learning Survey", source="openalex")
    b = _paper("deep reinforcement learning survey", source="arxiv")
    out = mgr._dedup([a, b])
    assert len(out) == 1


def test_dedup_keeps_distinct_papers():
    mgr = SearchManager()
    a = _paper("Graph Neural Networks", source="openalex")
    b = _paper("Transformer Architectures", source="arxiv")
    out = mgr._dedup([a, b])
    assert len(out) == 2


def test_search_all_cancels_and_drains_tasks_at_global_deadline(monkeypatch):
    """截止时间后必须取消并回收挂起请求，不能泄漏未 await 的协程。"""
    import asyncio
    import tools.search.manager as manager_mod

    cancelled = asyncio.Event()

    class FastBackend:
        async def search(self, query, limit=20):
            return [_paper("Fast result", source="fast")]

    class HangingBackend:
        async def search(self, query, limit=20):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setitem(manager_mod.BACKENDS, "fast-test", FastBackend())
    monkeypatch.setitem(manager_mod.BACKENDS, "hanging-test", HangingBackend())
    monkeypatch.setattr(manager_mod, "SEARCH_DEADLINE_SECONDS", 0.02)

    async def run():
        manager = SearchManager(
            enabled_sources=["fast-test", "hanging-test"],
            results_per_source=1,
            routing_mode="all_enabled",
        )
        papers = await manager.search_all(["query"])
        assert [paper.title for paper in papers] == ["Fast result"]
        assert cancelled.is_set()

    asyncio.run(run())
