"""1e integration tests: the multimodal pipeline chokepoint (_process_single_paper).

Covers the three behaviors 1e added over the old text-only reader:
- scanned-page recovery replaces the hard ``no_text`` failure,
- Stage 2/3 element understanding + persist runs on the parsed doc,
- the doc_fingerprint gate hydrates stored understanding for free on re-read.

All offline: the structure parser, scanned recovery, VLM analyzer, and vector
indexing are stubbed; a real in-memory Database exercises the SQLite persist +
fingerprint path for real.
"""

from __future__ import annotations

import asyncio

from agents import reader_agent as ra
from core.models import Paper
from tools.pdf.structure.models import PaperElement, ParsedPaperDocument, Section
from tools.storage.database import Database


class _Resp:
    def __init__(self, content):
        self.content = content


class UnderstandTracker:
    """Async stand-in for understand_elements that counts calls + fills a stub."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, elements, *, focus=""):
        self.calls += 1
        for el in elements:
            el.understanding = {"description": f"{el.kind} understood"}
        return elements


def _patch_extraction(monkeypatch, responder):
    """get_llm -> fake; index surfaces -> no-op (these tests are not about RAG)."""

    class _FakeLLM:
        async def ainvoke(self, messages):
            return _Resp(responder(messages[0].content))

    monkeypatch.setattr(ra, "get_llm", lambda name="light": _FakeLLM())
    monkeypatch.setattr(ra, "_index_paper", lambda *a, **k: None)
    monkeypatch.setattr(ra, "_index_elements_global", lambda *a, **k: None)


def _fake_parser(monkeypatch, doc):
    class _P:
        backend = "test"

        def parse(self, pdf_path, *, paper_id="", assets_dir=None):
            return doc

    monkeypatch.setattr(ra, "get_structure_parser", lambda: _P())


def _paper(pid="P1", abstract="Some real abstract text."):
    p = Paper(id=pid, title=f"Title {pid}", abstract=abstract)
    p.pdf_path = "/fake/p1.pdf"
    return p


def _element(eid, kind, ordinal):
    return PaperElement(
        element_id=eid, kind=kind, ordinal=ordinal, page=1, section="Method",
        caption=f"{kind} {ordinal}", asset_path=f"/tmp/{eid}.png",
        image_hash=f"h{ordinal}",
    )


# --- scanned-page recovery replaces the old hard no_text -----------------------

def test_scanned_paper_recovered_not_no_text(monkeypatch):
    """is_scanned + successful VLM-OCR recovery => extraction proceeds."""
    _patch_extraction(monkeypatch, lambda p: '{"methodology": "m from recovered text"}')
    doc = ParsedPaperDocument(
        sections=[Section("Abstract", "x")], raw_text="", is_scanned=True,
        doc_fingerprint="fp1",
    )
    _fake_parser(monkeypatch, doc)

    async def _recover(_path):
        return "Recovered abstract body. " * 30  # > 200 chars

    monkeypatch.setattr(ra, "recover_scanned_pages", _recover)
    tracker = UnderstandTracker()
    monkeypatch.setattr(ra, "understand_elements", tracker)

    paper = _paper(abstract="")  # no abstract fallback — must come from recovery
    db = Database(":memory:")
    summary, reason = asyncio.run(
        ra._process_single_paper(paper, ["methodology"], db, "general", "full")
    )
    assert reason is None
    assert summary is not None
    assert summary.methodology == "m from recovered text"
    assert summary.document_info["ocr_status"] == "recovered"
    assert summary.document_info["ocr_chars"] > 200
    assert summary.document_info["read_level"] == "full"


def test_scanned_recovery_empty_with_no_abstract_is_no_text(monkeypatch):
    """Recovery returns nothing + no abstract => degrade to no_text (not crash)."""
    _patch_extraction(monkeypatch, lambda p: '{"methodology": "x"}')
    doc = ParsedPaperDocument(raw_text="", is_scanned=True, doc_fingerprint="fp1")
    _fake_parser(monkeypatch, doc)

    async def _recover(_path):
        return ""

    monkeypatch.setattr(ra, "recover_scanned_pages", _recover)
    monkeypatch.setattr(ra, "understand_elements", UnderstandTracker())

    paper = _paper(abstract="")
    db = Database(":memory:")
    summary, reason = asyncio.run(
        ra._process_single_paper(paper, ["methodology"], db, "general", "full")
    )
    assert summary is None
    assert reason == "no_text"


# --- Stage 2/3 element understanding + persist --------------------------------

def test_understand_persist_saves_elements_with_fingerprint(monkeypatch):
    """Fresh doc (no stored fingerprint) => understand runs + elements saved."""
    paper = _paper()
    doc = ParsedPaperDocument(
        doc_fingerprint="fpAAA",
        elements=[_element("P1::figure::1", "figure", 1),
                  _element("P1::table::1", "table", 1)],
    )
    db = Database(":memory:")
    tracker = UnderstandTracker()
    monkeypatch.setattr(ra, "understand_elements", tracker)
    monkeypatch.setattr(ra, "_index_elements_global", lambda *a, **k: None)

    asyncio.run(ra._understand_and_persist_elements(paper, doc, db))

    assert tracker.calls == 1                       # VLM ran
    assert db.elements_fingerprint("P1") == "fpAAA"  # stamped
    rows = db.get_elements("P1")
    assert len(rows) == 2
    fig = next(r for r in rows if r["kind"] == "figure")
    assert fig["understanding"] == {"description": "figure understood"}


def test_fingerprint_hit_hydrates_without_vlm(monkeypatch):
    """Matching doc_fingerprint => understand NOT called, understanding hydrated."""
    paper = _paper()
    db = Database(":memory:")
    # Seed the global table with already-understood elements + a fingerprint.
    seed = ParsedPaperDocument(
        doc_fingerprint="fpAAA",
        elements=[_element("P1::figure::1", "figure", 1)],
    )
    seed.elements[0].understanding = {"description": "previously understood"}
    db.save_elements("P1", seed.elements, "fpAAA")

    # Fresh re-parse: same fingerprint, empty understanding on the fresh element.
    fresh = ParsedPaperDocument(
        doc_fingerprint="fpAAA",
        elements=[_element("P1::figure::1", "figure", 1)],  # understanding=None
    )
    tracker = UnderstandTracker()
    monkeypatch.setattr(ra, "understand_elements", tracker)
    monkeypatch.setattr(ra, "_index_elements_global", lambda *a, **k: None)

    asyncio.run(ra._understand_and_persist_elements(paper, fresh, db))

    assert tracker.calls == 0                                 # zero VLM tokens
    assert fresh.elements[0].understanding == {"description": "previously understood"}
    # And no re-save happened (row count unchanged, fingerprint still fpAAA).
    assert len(db.get_elements("P1")) == 1


def test_summary_carries_element_refs(monkeypatch):
    """A successful full read attaches light element refs to the summary."""
    _patch_extraction(monkeypatch, lambda p: '{"methodology": "m"}')
    doc = ParsedPaperDocument(
        sections=[Section("Method", "method body " * 50)],
        raw_text="method body " * 50,
        doc_fingerprint="fp1",
        elements=[_element("P1::figure::1", "figure", 1)],
    )
    _fake_parser(monkeypatch, doc)
    monkeypatch.setattr(ra, "understand_elements", UnderstandTracker())
    monkeypatch.setattr(ra, "recover_scanned_pages", lambda _p: "")  # not scanned anyway

    paper = _paper()
    db = Database(":memory:")
    summary, reason = asyncio.run(
        ra._process_single_paper(paper, ["methodology"], db, "general", "full")
    )
    assert reason is None
    assert summary.elements
    assert summary.elements[0]["element_id"] == "P1::figure::1"
    assert summary.elements[0]["kind"] == "figure"


def test_summary_cache_hit_restores_persisted_elements_without_parse_or_vlm(monkeypatch):
    """Summary cache and element cache are independent; a hit restores both."""
    import json

    paper = _paper()
    db = Database(":memory:")
    db.save_cached_summary(
        paper.id, "general", "full",
        json.dumps({
            "paper_id": paper.id, "methodology": "cached method",
            "full_text": "cached full text body " * 20,
        }),
    )
    element = _element("P1::figure::1", "figure", 1)
    element.understanding = {"description": "cached diagram"}
    db.save_elements(paper.id, [element], "fp-cache")

    monkeypatch.setattr(
        ra, "get_structure_parser",
        lambda: (_ for _ in ()).throw(AssertionError("summary hit reparsed PDF")),
    )
    monkeypatch.setattr(
        ra, "understand_elements",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("summary hit called VLM")),
    )
    indexed = []
    monkeypatch.setattr(ra, "_index_elements_global", lambda p, els: indexed.extend(els))
    monkeypatch.setattr(ra, "_index_paper", lambda *a, **k: None)

    summary, reason = asyncio.run(
        ra._process_single_paper(paper, ["methodology"], db, "general", "full")
    )
    assert reason is None
    assert summary.methodology == "cached method"
    assert summary.elements == [{
        "element_id": "P1::figure::1", "kind": "figure", "page": 1,
        "section": "Method", "caption": "figure 1",
    }]
    assert len(indexed) == 1
    assert indexed[0].understanding == {"description": "cached diagram"}


def test_document_info_marks_digital_pdf_ocr_not_needed():
    paper = Paper(id="P1", title="digital", pdf_path="/tmp/digital.pdf")
    doc = ParsedPaperDocument(
        raw_text="body " * 100, parser_backend="docling", page_count=8,
        sections=[Section("1 Introduction", "text", 1, 2)], is_scanned=False,
        ocr_status="not_needed", ocr_chars=0,
    )
    info = ra._document_info(paper, doc, read_level="full", text_chars=len(doc.raw_text))
    assert info == {
        "read_level": "full", "pdf_fetched": True, "parse_status": "parsed_full",
        "parser_backend": "docling",
        "page_count": 8, "text_chars": len(doc.raw_text), "section_count": 1,
        "is_scanned": False, "ocr_status": "not_needed", "ocr_chars": 0,
        "element_count": 0, "vision_understood_count": 0,
    }


def test_parse_and_understand_marks_empty_scanned_ocr_degraded(monkeypatch):
    doc = ParsedPaperDocument(raw_text="", is_scanned=True, doc_fingerprint="fp-empty")
    _fake_parser(monkeypatch, doc)

    async def _recover(_path):
        return ""

    monkeypatch.setattr(ra, "recover_scanned_pages", _recover)
    paper = _paper(abstract="")
    db = Database(":memory:")
    parsed = asyncio.run(ra.parse_and_understand(paper, db))
    assert parsed is doc
    assert parsed.ocr_status == "unavailable_or_empty"
    assert parsed.ocr_chars == 0


def test_parse_and_understand_digital_pdf_skips_vlm_ocr(monkeypatch):
    doc = ParsedPaperDocument(raw_text="digital body " * 50, is_scanned=False,
                              doc_fingerprint="fp-digital")
    _fake_parser(monkeypatch, doc)

    async def _forbidden(_path):
        raise AssertionError("digital PDF must not invoke Stage 1.5 VLM-OCR")

    monkeypatch.setattr(ra, "recover_scanned_pages", _forbidden)
    paper = _paper()
    parsed = asyncio.run(ra.parse_and_understand(paper, Database(":memory:")))
    assert parsed.ocr_status == "not_needed"
    assert parsed.ocr_chars == 0
