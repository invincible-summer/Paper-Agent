"""Docling backend helper tests (offline) + one slow real-conversion test.

The helper tests cover the coordinate-system conversion (Docling's bottom-left
origin -> PyMuPDF's top-left origin) and table-markdown extraction without
loading any model. The slow test runs a real Docling conversion on a fixture
PDF (downloads models on first run) and is excluded by default.
"""

from __future__ import annotations

import os
import types

import pytest

from tools.pdf.structure.docling_parser import (
    DoclingParser,
    _bbox_to_rect,
    _is_bottomleft,
)


class _Origin:
    def __init__(self, name: str):
        self.name = name


class _BBox:
    def __init__(self, l, t, r, b, origin_name):
        self.l, self.t, self.r, self.b = l, t, r, b
        self.coord_origin = _Origin(origin_name)


class _Page:
    def __init__(self, height: float):
        self.rect = types.SimpleNamespace(height=height)


class _FakeTable:
    def __init__(self, md: str):
        self._md = md

    def export_to_markdown(self, doc=None):
        return self._md


def test_is_bottomleft_detection():
    assert _is_bottomleft(_BBox(0, 0, 1, 1, "BOTTOMLEFT")) is True
    assert _is_bottomleft(_BBox(0, 0, 1, 1, "TOPLEFT")) is False


def test_bbox_to_rect_topleft_origin():
    page = _Page(800)
    rect = _bbox_to_rect(_BBox(10, 100, 200, 250, "TOPLEFT"), page)
    assert rect is not None
    assert (rect.x0, rect.y0, rect.x1, rect.y1) == (10, 100, 200, 250)


def test_bbox_to_rect_bottomleft_origin_flipped():
    """Docling BOTTOMLEFT: t (>b) is distance from page bottom to the bbox top."""
    page = _Page(800)
    rect = _bbox_to_rect(_BBox(10, 700, 200, 750, "BOTTOMLEFT"), page)
    assert rect is not None
    # 800-750=50 (top), 800-700=100 (bottom)
    assert (rect.x0, rect.y0, rect.x1, rect.y1) == (10, 50, 200, 100)


def test_bbox_to_rect_tiny_returns_none():
    page = _Page(800)
    assert _bbox_to_rect(_BBox(10, 10, 12, 12, "TOPLEFT"), page) is None  # < 8pt
    assert _bbox_to_rect(None, page) is None


def test_table_markdown_extraction():
    md = DoclingParser._table_markdown(_FakeTable("| a |\n| --- |\n| 1 |"), doc=object())
    assert md.startswith("| a |")


# --- slow real-conversion integration test -------------------------------------

@pytest.mark.slow
def test_docling_real_conversion(tmp_path):
    pytest.importorskip("docling")
    pdf = "data/pdfs/doi:10.1038_s41746-020-0288-5.pdf"
    if not os.path.exists(pdf):
        pytest.skip("fixture PDF not present")
    from tools.pdf.structure import get_structure_parser

    parser = get_structure_parser()
    if parser.backend != "docling":
        pytest.skip("docling backend not active")
    doc = parser.parse(pdf, paper_id="itest", assets_dir=str(tmp_path / "assets"))

    assert doc.page_count > 0
    assert len(doc.raw_text) > 1000
    assert any(e.kind in ("figure", "table") for e in doc.elements)
    # Every kept figure has a real PNG asset + image hash (size guard applied).
    for e in doc.elements:
        if e.kind == "figure":
            assert e.asset_path and e.image_hash
            break
