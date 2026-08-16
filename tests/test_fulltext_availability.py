"""Verified OA full-text availability: lightweight PDF probes, no downloads."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import agents.search_agent as search_mod
import tools.pdf.availability as avail
from core.models import Paper
from core.reading_policy import (
    FULLTEXT_STATUS_AVAILABLE,
    FULLTEXT_STATUS_UNAVAILABLE,
    fulltext_available,
)


def _settings(tmp_path: Path):
    return SimpleNamespace(
        search=SimpleNamespace(
            fulltext_verify_timeout_seconds=5,
            fulltext_status_ttl_days=30,
        ),
        reader=SimpleNamespace(pdf_dir=str(tmp_path / "pdfs"), assets_dir=str(tmp_path / "assets")),
        storage=SimpleNamespace(sqlite_path=str(tmp_path / "meta.db")),
    )


class _ProbeFetcher:
    """Fake fetcher that exposes candidate URLs but cannot download anything."""

    def __init__(self, url_of, probe_of, *a, **k):
        self.url_of = url_of
        self.probe_of = probe_of
        self.probe_calls = 0
        self.candidate_calls = 0

    async def candidate_urls(self, paper):
        self.candidate_calls += 1
        urls = self.url_of(paper)
        return urls

    async def close(self):
        pass

    async def probe(self, url):
        self.probe_calls += 1
        return self.probe_of(url)


def test_probe_marks_available_without_downloading(monkeypatch, tmp_path):
    """A live PDF head is enough; the full PDF must not be downloaded."""
    seen = {"download": False}

    async def probe(url):
        return (True, "ok") if url == "https://oa.example/a.pdf" else (False, "not_pdf")

    class Fetcher(_ProbeFetcher):
        def __init__(self, *a, **k):
            super().__init__(
                lambda p: [p.pdf_url] if p.pdf_url else [], probe, *a, **k)

        async def fetch(self, paper):
            seen["download"] = True
            raise AssertionError("availability probe must not download the PDF")

    monkeypatch.setattr(avail, "PDFFetcher", Fetcher)
    monkeypatch.setattr(avail, "probe_pdf_url", probe)
    monkeypatch.setattr(avail, "get_settings", lambda: _settings(tmp_path))

    papers = [
        Paper(id="OA", title="open access", pdf_url="https://oa.example/a.pdf"),
        Paper(id="PAYWALL", title="paywalled",
              pdf_url="https://publisher.example/doi/pdf/landing"),
    ]
    statuses = asyncio.run(avail.verify_papers_fulltext(papers))

    assert statuses["OA"] == FULLTEXT_STATUS_AVAILABLE
    assert statuses["PAYWALL"] == FULLTEXT_STATUS_UNAVAILABLE
    assert papers[0].fulltext_status == FULLTEXT_STATUS_AVAILABLE
    assert papers[0].pdf_path is None  # probe only — nothing was saved locally
    assert not (tmp_path / "pdfs").exists()  # no download directory, no temp files
    assert not seen["download"]

    # The stored result must record WHY the paper was classified, so an
    # HTML/abstract landing page is auditable as "not a PDF", never as success.
    from tools.storage.database import Database
    db = Database(_settings(tmp_path).storage.sqlite_path)
    rows = db.get_fulltext_statuses(["OA", "PAYWALL"])
    assert "oa_url_probe_verified" in rows["OA"]["evidence"]
    assert "not_pdf" in rows["PAYWALL"]["evidence"]
    db.close()


def test_url_only_is_never_available(monkeypatch, tmp_path):
    """Regression: the genealogy used to mark any http(s) pdf_url as full text."""
    paper = Paper(id="P", title="paywalled",
                  pdf_url="https://dl.acm.org/doi/pdf/10.1145/1")
    assert paper.fulltext_status == "unknown"
    assert not fulltext_available(paper)


def test_transient_probe_error_stays_unknown(monkeypatch, tmp_path):
    """Network hiccups must not be persisted as a verified 'unavailable'."""

    async def boom(url):
        raise OSError("transient")

    class Fetcher(_ProbeFetcher):
        def __init__(self, *a, **k):
            super().__init__(lambda p: [p.pdf_url] if p.pdf_url else [],
                             lambda url: (_ for _ in ()).throw(OSError("transient")),
                             *a, **k)

    monkeypatch.setattr(avail, "PDFFetcher", Fetcher)
    monkeypatch.setattr(avail, "probe_pdf_url", boom)
    monkeypatch.setattr(avail, "get_settings", lambda: _settings(tmp_path))

    paper = Paper(id="P", pdf_url="https://example.org/p.pdf")
    statuses = asyncio.run(avail.verify_papers_fulltext([paper]))
    assert statuses["P"] == "unknown"
    assert paper.fulltext_status == "unknown"


def test_cached_local_pdf_is_available_without_network(monkeypatch, tmp_path):
    """An already-downloaded local PDF is directly available."""
    pdf = tmp_path / "cached.pdf"
    pdf.write_bytes(b"%PDF-1.4 local")

    class ForbiddenFetcher:
        def __init__(self, *a, **k):
            pass

        async def candidate_urls(self, paper):
            raise AssertionError("local PDF must not trigger network probes")

        async def close(self):
            pass

    monkeypatch.setattr(avail, "PDFFetcher", ForbiddenFetcher)
    monkeypatch.setattr(avail, "get_settings", lambda: _settings(tmp_path))

    paper = Paper(id="A", pdf_path=str(pdf))
    statuses = asyncio.run(avail.verify_papers_fulltext([paper]))
    assert statuses["A"] == FULLTEXT_STATUS_AVAILABLE
    assert paper.fulltext_status == FULLTEXT_STATUS_AVAILABLE


def test_verified_statuses_are_cached_and_reused(monkeypatch, tmp_path):
    """A fresh DB row avoids re-probing on the next search."""
    calls = {"n": 0}

    async def probe(url):
        calls["n"] += 1
        return (True, "ok") if url == "https://oa.example/a.pdf" else (False, "not_pdf")

    class WorkingFetcher(_ProbeFetcher):
        def __init__(self, *a, **k):
            super().__init__(lambda p: [p.pdf_url] if p.pdf_url else [], probe, *a, **k)

    class ForbiddenFetcher:
        def __init__(self, *a, **k):
            pass

        async def candidate_urls(self, paper):
            calls["n"] += 1
            raise AssertionError("fresh cache row should prevent a re-probe")

        async def close(self):
            pass

    monkeypatch.setattr(avail, "PDFFetcher", WorkingFetcher)
    monkeypatch.setattr(avail, "probe_pdf_url", probe)
    monkeypatch.setattr(avail, "get_settings", lambda: _settings(tmp_path))

    first = asyncio.run(avail.verify_papers_fulltext([
        Paper(id="A", pdf_url="https://oa.example/a.pdf"),
        Paper(id="B", pdf_url="https://paywall.example/b"),
    ]))
    assert first["A"] == FULLTEXT_STATUS_AVAILABLE
    assert first["B"] == FULLTEXT_STATUS_UNAVAILABLE
    assert calls["n"] == 2  # one probe per paper on the first pass

    monkeypatch.setattr(avail, "PDFFetcher", ForbiddenFetcher)
    papers = [Paper(id="A", pdf_url="https://oa.example/a.pdf"),
              Paper(id="B", pdf_url="https://paywall.example/b")]
    second = asyncio.run(avail.verify_papers_fulltext(papers))
    assert second["A"] == FULLTEXT_STATUS_AVAILABLE
    assert second["B"] == FULLTEXT_STATUS_UNAVAILABLE
    assert calls["n"] == 2  # no new network calls


def test_unavailable_is_rechecked_when_a_new_pdf_url_appears(monkeypatch, tmp_path):
    """A previously failed paper with a newly discovered OA link must re-probe."""
    calls = {"n": 0}

    async def probe(url):
        calls["n"] += 1
        return (True, "ok") if url == "https://new-oa.example/b.pdf" else (False, "not_pdf")

    class Fetcher(_ProbeFetcher):
        def __init__(self, *a, **k):
            super().__init__(lambda p: [p.pdf_url] if p.pdf_url else [], probe, *a, **k)

    monkeypatch.setattr(avail, "PDFFetcher", Fetcher)
    monkeypatch.setattr(avail, "probe_pdf_url", probe)
    monkeypatch.setattr(avail, "get_settings", lambda: _settings(tmp_path))

    old = asyncio.run(avail.verify_papers_fulltext([
        Paper(id="B", pdf_url="https://paywall.example/b")]))
    assert old["B"] == FULLTEXT_STATUS_UNAVAILABLE

    out = asyncio.run(avail.verify_papers_fulltext([
        Paper(id="B", pdf_url="https://new-oa.example/b.pdf")]))
    assert out["B"] == FULLTEXT_STATUS_AVAILABLE
    assert calls["n"] == 2  # one failed probe, one re-probe for the new URL


def test_search_agent_pipeline_runs_fulltext_verification(monkeypatch):
    """search_papers must annotate every tiered paper with a verified status."""
    papers = [
        Paper(id="P1", title="OA paper", relevance_score=0.9),
        Paper(id="P2", title="paywalled", relevance_score=0.8),
    ]

    async def fake_understand(topic, conception, language):
        return {
            "research_goal": topic,
            "sub_directions": [{"name": topic, "queries_en": [topic], "queries_zh": []}],
            "queries": [topic],
        }

    class FakeManager:
        def __init__(self, enabled_sources=None, results_per_source=20):
            pass

        async def search_all(self, queries, progress_callback=None):
            return list(papers)

    class FakeDB:
        def __init__(self, db_path=None, storage_context=None):
            pass

        def save_papers(self, *_):
            pass

        def close(self):
            pass

    class FakeVS:
        def __init__(self, storage_context=None):
            pass

        def upsert_paper_summary(self, *_a, **_k):
            pass

    async def fake_verify(papers_to_check, **kwargs):
        statuses = {}
        for p in papers_to_check:
            p.fulltext_status = (
                FULLTEXT_STATUS_AVAILABLE if p.id == "P1"
                else FULLTEXT_STATUS_UNAVAILABLE
            )
            statuses[p.id] = p.fulltext_status
        return statuses

    monkeypatch.setattr(search_mod, "_understand", fake_understand)
    monkeypatch.setattr(search_mod, "SearchManager", FakeManager)
    monkeypatch.setattr(search_mod, "Database", FakeDB)
    monkeypatch.setattr(search_mod, "_embed_ok", lambda _query: True)
    monkeypatch.setattr(
        search_mod, "rerank_papers",
        lambda scored, _query: sorted(scored, key=lambda p: p.relevance_score, reverse=True),
    )
    monkeypatch.setattr(avail, "verify_papers_fulltext", fake_verify)
    import tools.storage.vectorstore as vs_mod
    monkeypatch.setattr(vs_mod, "VectorStore", FakeVS)
    monkeypatch.setattr(search_mod, "get_settings", lambda: SimpleNamespace(
        search=SimpleNamespace(
            verify_fulltext=True,
            results_per_source=20,
            sources={"openalex": True},
        ),
        storage=SimpleNamespace(sqlite_path=":memory:"),
    ))

    state = asyncio.run(search_mod.search_agent({"topic": "graph neural networks"}))
    assert [p.id for p in state["papers"]] == ["P1", "P2"]
    assert state["fulltext_statuses"] == {"P1": "available", "P2": "unavailable"}
    assert state["papers"][0].fulltext_status == FULLTEXT_STATUS_AVAILABLE
    assert state["papers"][1].fulltext_status == FULLTEXT_STATUS_UNAVAILABLE
