"""PyMuPDF fallback parser + factory tests (offline; synthetic PDF, no models)."""

from __future__ import annotations

import sys

import fitz
import pytest

import tools.pdf.structure.base as base
from core.config import Settings
from tools.pdf.structure.pymupdf_parser import (
    PyMuPDFStructureParser,
    _rows_to_markdown,
)


def _make_two_page_pdf(path: str) -> None:
    """Page 1+2 each carry a repeating running header + a section + an image."""
    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 150))  # a real embedded image
    for i, (title, body) in enumerate(
        [("Introduction", "This is the intro body text about figures."),
         ("Method", "This is the method body text.")],
        start=1,
    ):
        page = doc.new_page()
        page.insert_text((72, 30), "Running Header Text", fontsize=8)  # repeating header
        page.insert_text((72, 80), title, fontsize=14)
        page.insert_text((72, 110), body, fontsize=10)
        page.insert_image(fitz.Rect(72, 150, 272, 300), pixmap=pix)
    doc.save(path)
    doc.close()


def test_pymupdf_parser_sections_pages_and_header_strip(tmp_path):
    pdf = str(tmp_path / "t.pdf")
    _make_two_page_pdf(pdf)

    parser = PyMuPDFStructureParser()
    doc = parser.parse(pdf, paper_id="pid", assets_dir=str(tmp_path / "assets"))

    assert doc.parser_backend == "pymupdf"
    assert doc.page_count == 2
    # Page numbers are preserved per section (the old parser flattened pages).
    titles = {s.title: s for s in doc.sections}
    assert "Introduction" in titles and titles["Introduction"].page_start == 1
    assert "Method" in titles and titles["Method"].page_start == 2
    # Repeating running header is detected and removed.
    assert "Running Header Text" not in doc.raw_text
    assert "intro body" in doc.raw_text and "method body" in doc.raw_text
    # Fingerprint is populated for cache versioning.
    assert len(doc.doc_fingerprint) == 64


def test_pymupdf_parser_extracts_figure_elements(tmp_path):
    pdf = str(tmp_path / "t.pdf")
    _make_two_page_pdf(pdf)

    parser = PyMuPDFStructureParser()
    doc = parser.parse(pdf, paper_id="pid", assets_dir=str(tmp_path / "assets"))

    figs = [e for e in doc.elements if e.kind == "figure"]
    assert len(figs) >= 1
    fig = figs[0]
    assert fig.asset_path and fig.image_hash
    assert fig.element_id.startswith("pid::figure::")
    assert fig.page in (1, 2)
    # The cropped asset is a valid PNG.
    with open(fig.asset_path, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"


def test_rows_to_markdown():
    md = _rows_to_markdown([["Method", "Acc"], ["A", "92%"], ["B", "90%"]])
    lines = md.splitlines()
    assert lines[0] == "| Method | Acc |"
    assert lines[1].startswith("| ---")
    assert lines[2] == "| A | 92% |"


# --- factory / fallback --------------------------------------------------------

def test_factory_returns_pymupdf_when_configured(monkeypatch):
    base._PARSER = None
    s = Settings()
    s.reader.structure_backend = "pymupdf"
    monkeypatch.setattr(base, "get_settings", lambda: s)
    p = base.get_structure_parser()
    assert p.backend == "pymupdf"


def test_factory_falls_back_when_docling_module_unavailable(monkeypatch):
    """If the Docling module import fails, the factory must still return a
    working (PyMuPDF) parser rather than raising."""
    base._PARSER = None
    s = Settings()
    s.reader.structure_backend = "docling"
    monkeypatch.setattr(base, "get_settings", lambda: s)
    # Simulate the docling backend submodule failing to import (e.g. docling not
    # installed). A None entry in sys.modules makes `from ... import` raise.
    monkeypatch.setitem(sys.modules, "tools.pdf.structure.docling_parser", None)
    p = base.get_structure_parser()
    assert p.backend == "pymupdf"
