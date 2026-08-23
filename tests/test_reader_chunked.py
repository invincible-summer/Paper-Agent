"""Phase 0 reader tests: section-aware chunked extraction, cache, failures.

All offline: get_llm is monkeypatched to a stub whose response depends on the
prompt content (so chunked map-reduce is exercised deterministically).
"""

from __future__ import annotations

import asyncio

import pytest

from agents import reader_agent as ra
from core.models import Paper, PaperSummary
from tools.pdf.structure.models import ParsedPaperDocument, Section
from tools.storage.database import Database


class _Resp:
    def __init__(self, content):
        self.content = content


async def _noop_understand(elements, *, focus=""):
    """Offline stand-in for the VLM element analyzer (never hits the network)."""
    return elements


def _patch_llm(monkeypatch, responder):
    """Patch get_llm in reader_agent; responder(prompt_text) -> str response."""

    class _FakeLLM:
        async def ainvoke(self, messages):
            prompt = messages[0].content
            return _Resp(responder(prompt))

    monkeypatch.setattr(ra, "get_llm", lambda name="light": _FakeLLM())
    # Indexing into the vector store would otherwise load the real embedding
    # model / hit the network. These tests exercise extraction, not RAG or the
    # VLM, so stub the multimodal + indexing surfaces to no-ops.
    monkeypatch.setattr(ra, "_index_paper", lambda *a, **k: None)
    monkeypatch.setattr(ra, "_index_elements_global", lambda *a, **k: None)
    monkeypatch.setattr(ra, "understand_elements", _noop_understand)


def _patch_parser(monkeypatch, parsed):
    """Make get_structure_parser() return a fake that yields `parsed`."""

    class _FakeParser:
        backend = "test"

        def parse(self, pdf_path, *, paper_id="", assets_dir=None):
            return parsed

    monkeypatch.setattr(ra, "get_structure_parser", lambda: _FakeParser())


def _paper(pid="P1", abstract="Short abstract."):
    return Paper(id=pid, title=f"Title {pid}", abstract=abstract)


def test_section_chunked_extraction_reads_latter_half(monkeypatch):
    """Full mode with >1 chunk must surface info that lived past the old 8000-char cut."""

    def responder(prompt):
        # Part 2 carries the methodology marker; the single-shot path would
        # never have seen it.
        if "part 2 of 2" in prompt.lower() or "SECRET_METHOD_MARKER" in prompt:
            return '{"methodology": "SECRET_METHOD_MARKER found", "key_findings": ["late result"]}'
        return '{"methodology": "intro method", "key_findings": ["early result"]}'

    _patch_llm(monkeypatch, responder)

    big_method = "x" * (ra.get_settings().reader.read_chunk_chars)  # force 2 chunks
    parsed = ParsedPaperDocument(
        sections=[Section("Introduction", "intro body"), Section("Method", big_method)],
        references=[],
        raw_text="intro body\n" + big_method,
    )
    _patch_parser(monkeypatch, parsed)

    paper = _paper()
    paper.source = "upload"
    paper.pdf_path = "/fake/p1.pdf"
    db = Database(":memory:")
    summary, reason = asyncio.run(
        ra._process_single_paper(paper, ["methodology", "key_findings"], db, "general", "full")
    )

    assert reason is None
    assert "SECRET_METHOD_MARKER" in summary.methodology
    assert "late result" in summary.key_findings
    assert "early result" in summary.key_findings  # merged from chunk 1


def test_json_failure_retries_then_records_failure(monkeypatch):
    """Non-JSON twice -> extraction_failed, paper excluded from summaries."""
    calls = {"n": 0}

    def responder(prompt):
        calls["n"] += 1
        return "this is not json at all"

    _patch_llm(monkeypatch, responder)

    paper = _paper(abstract="Some real abstract text here.")
    db = Database(":memory:")
    summary, reason = asyncio.run(
        ra._process_single_paper(paper,["methodology"], db, "general", "abstract")
    )

    assert summary is None
    assert reason == "extraction_failed"
    assert calls["n"] == 2  # initial + one strict retry


def test_json_retry_succeeds_on_second_call(monkeypatch):
    """First call returns prose, strict retry returns valid JSON -> success."""
    calls = {"n": 0}

    def responder(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json"
        return '{"methodology": "recovered method"}'

    _patch_llm(monkeypatch, responder)

    paper = _paper(abstract="Some abstract.")
    db = Database(":memory:")
    summary, reason = asyncio.run(
        ra._process_single_paper(paper,["methodology"], db, "general", "abstract")
    )

    assert reason is None
    assert summary.methodology == "recovered method"
    assert calls["n"] == 2


def test_empty_shell_detected_as_failure(monkeypatch):
    """Valid JSON but all fields empty -> empty_shell failure (no fake success)."""

    def responder(prompt):
        return '{"methodology": "", "key_findings": [], "references_raw": []}'

    _patch_llm(monkeypatch, responder)

    paper = _paper(abstract="Some abstract.")
    db = Database(":memory:")
    summary, reason = asyncio.run(
        ra._process_single_paper(paper,["methodology", "key_findings"], db, "general", "abstract")
    )

    assert summary is None
    assert reason == "empty_shell"


def test_cache_hit_skips_llm(monkeypatch):
    """Second run with a warm cache must make zero LLM calls."""
    calls = {"n": 0}

    def responder(prompt):
        calls["n"] += 1
        return '{"methodology": "cached method", "key_findings": ["k1"]}'

    _patch_llm(monkeypatch, responder)

    paper = _paper(abstract="Abstract body.")
    db = Database(":memory:")

    s1, r1 = asyncio.run(
        ra._process_single_paper(paper,["methodology", "key_findings"], db, "general", "abstract")
    )
    assert r1 is None and calls["n"] == 1

    s2, r2 = asyncio.run(
        ra._process_single_paper(paper,["methodology", "key_findings"], db, "general", "abstract")
    )
    assert r2 is None
    assert calls["n"] == 1  # no second call: cache served
    assert s2.methodology == s1.methodology


def test_no_text_returns_no_text_failure(monkeypatch):
    _patch_llm(monkeypatch, lambda p: '{"methodology": "x"}')

    paper = Paper(id="Pempty", title="Empty", abstract="")
    db = Database(":memory:")
    summary, reason = asyncio.run(
        ra._process_single_paper(paper,["methodology"], db, "general", "abstract")
    )
    assert summary is None
    assert reason == "no_text"


def test_full_mode_without_oa_pdf_stays_abstract_in_cache(monkeypatch):
    """A full attempt with no PDF must not store the abstract under ``full``."""
    calls = {"n": 0}

    def responder(prompt):
        calls["n"] += 1
        return '{"methodology": "abstract method", "key_findings": ["f"]}'

    _patch_llm(monkeypatch, responder)

    paper = _paper(abstract="Abstract body long enough for extraction.")
    paper.pdf_path = None
    db = Database(":memory:")
    summary, reason = asyncio.run(
        ra._process_single_paper(paper, ["methodology", "key_findings"],
                                 db, "general", "full")
    )

    assert reason is None
    assert summary is not None
    assert not summary.full_text
    assert db.get_cached_summary(paper.id, "general", "full") is None
    assert db.get_cached_summary(paper.id, "general", "abstract") is not None
    assert calls["n"] == 1


def test_network_full_cache_is_ignored(monkeypatch):
    """A legacy full-mode row cannot promote a network paper above its abstract."""
    import json

    calls = {"n": 0}

    def responder(prompt):
        calls["n"] += 1
        return '{"methodology": "fresh abstract method", "key_findings": ["f"]}'

    _patch_llm(monkeypatch, responder)

    paper = _paper(abstract="Abstract body long enough for extraction.")
    paper.pdf_path = None
    db = Database(":memory:")
    db.save_cached_summary(
        paper.id, "general", "full",
        json.dumps({"paper_id": paper.id, "methodology": "poisoned"}),
    )

    summary, reason = asyncio.run(
        ra._process_single_paper(paper, ["methodology", "key_findings"],
                                 db, "general", "full")
    )

    assert reason is None
    assert summary is not None
    assert not summary.full_text
    assert calls["n"] == 1  # poisoned cache did not short-circuit the fetch
    assert db.get_cached_summary(paper.id, "general", "abstract") is not None


def test_network_full_cache_containing_abstract_is_ignored(monkeypatch):
    """Old code could store the abstract itself as full_text after a parse
    failure; that row must not be served as a full-text cache hit."""
    import json

    calls = {"n": 0}

    def responder(prompt):
        calls["n"] += 1
        return '{"methodology": "fresh abstract", "key_findings": ["f"]}'

    _patch_llm(monkeypatch, responder)

    paper = _paper(abstract="Abstract body long enough for extraction.")
    paper.pdf_path = None
    db = Database(":memory:")
    db.save_cached_summary(
        paper.id, "general", "full",
        json.dumps({
            "paper_id": paper.id,
            "methodology": "poisoned",
            "full_text": paper.abstract,  # abstract masquerading as full text
        }),
    )

    summary, reason = asyncio.run(
        ra._process_single_paper(paper, ["methodology", "key_findings"],
                                 db, "general", "full")
    )

    assert reason is None
    assert summary is not None and not summary.full_text
    assert calls["n"] == 1


def test_downloaded_pdf_parse_failure_does_not_fake_full_text(monkeypatch):
    """PDF path present but parser fails → abstract is not labelled full_text."""
    async def fake_parse_failure(*args, **kwargs):
        return None

    monkeypatch.setattr(ra, "parse_and_understand", fake_parse_failure)
    _patch_llm(monkeypatch, lambda p: '{"methodology": "abstract fallback"}')

    paper = _paper(abstract="Abstract body long enough for extraction.")
    paper.pdf_path = "/fake/paid-landing-page.pdf"
    db = Database(":memory:")
    summary, reason = asyncio.run(
        ra._process_single_paper(paper, ["methodology"], db, "general", "full")
    )

    assert reason is None
    assert summary is not None
    assert not summary.full_text
    assert db.get_cached_summary(paper.id, "general", "full") is None
    assert db.get_cached_summary(paper.id, "general", "abstract") is not None


def test_reader_agent_collects_failures(monkeypatch, tmp_path):
    """End-to-end: mixed success/failure populates paper_summaries + read_failures."""
    # Point the agent at a throwaway DB so prior cached summaries don't leak in.
    monkeypatch.setattr(ra.get_settings().storage, "sqlite_path", str(tmp_path / "t.db"))

    def responder(prompt):
        # Route by paper abstract present in the prompt (concurrency-safe).
        if "abstract A" in prompt:
            return '{"methodology": "ok method", "key_findings": ["f"]}'
        return "garbage"

    _patch_llm(monkeypatch, responder)

    state = {
        "topic": "t",
        "language": "en",
        "field_profile": "general",
        "papers": [_paper("A", "abstract A"), _paper("B", "abstract B")],
        "read_mode": "abstract",
    }
    result = asyncio.run(ra.reader_agent(state, core_limit=2, read_mode="abstract"))

    assert "A" in result["paper_summaries"]
    assert "B" not in result["paper_summaries"]
    failures = result["read_failures"]
    assert len(failures) == 1
    assert failures[0]["paper_id"] == "B"
    assert failures[0]["reason"] == "extraction_failed"


def test_build_chunks_orders_and_packs():
    parsed = ParsedPaperDocument(
        sections=[
            Section("Conclusion", "concl " * 100),
            Section("Abstract", "abstract " * 100),
            Section("Method", "method " * 5000),
        ],
        references=[],
        raw_text="x" * 20000,
    )
    chunks = ra._build_chunks(parsed, max_chars=2000)
    assert len(chunks) >= 2
    # abstract (highest priority) must lead the first chunk
    assert chunks[0].lower().startswith("[abstract]")


def test_build_chunks_falls_back_to_raw_text_when_sections_are_stale():
    """Scanned-page recovery appends OCR text to raw_text, not sections; the
    chunker must still feed that recovered body to extraction."""
    recovered = ("Recovered OCR body " * 200) + " LATE_METHOD_MARKER"
    parsed = ParsedPaperDocument(
        sections=[Section("Abstract", "tiny section only")],
        references=[],
        raw_text=recovered,
    )
    chunks = ra._build_chunks(parsed, max_chars=2000)
    assert len(chunks) >= 1
    assert "LATE_METHOD_MARKER" in "\n".join(chunks)


def test_merge_extractions_unions_and_dedups():
    parts = [
        {"methodology": "shared approach", "key_findings": ["a", "b"], "references_raw": ["r1"]},
        {"methodology": "shared approach", "key_findings": ["b", "c"], "references_raw": ["r2"]},
    ]
    merged = ra._merge_extractions(parts, ["methodology", "key_findings"])
    assert merged["methodology"] == "shared approach"  # deduped string
    assert merged["key_findings"] == ["a", "b", "c"]   # unioned + deduped list
    assert merged["references_raw"] == ["r1", "r2"]


def test_summary_to_dict_roundtrip():
    s = PaperSummary(
        paper_id="P1", methodology="m", key_findings=["a"],
        references=[__import__("core.models", fromlist=["Reference"]).Reference(raw_text="r1")],
        full_text="full",
        section_outline=[{"title": "1 Introduction", "page_start": 1, "page_end": 2}],
        document_info={"read_level": "full", "page_count": 2, "ocr_status": "not_needed"},
    )
    d = s.to_dict()
    assert d["paper_id"] == "P1"
    assert d["methodology"] == "m"
    assert d["key_findings"] == ["a"]
    assert d["references_raw"] == ["r1"]
    assert d["full_text"] == "full"
    # round-trip via cache path
    back = ra._summary_from_dict("P1", d)
    assert back.methodology == "m"
    assert back.key_findings == ["a"]
    assert back.full_text == "full"
    assert back.references[0].raw_text == "r1"
    assert back.section_outline[0]["title"] == "1 Introduction"
    assert back.document_info["ocr_status"] == "not_needed"
