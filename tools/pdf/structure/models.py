"""Structure-layer data models (DESIGN: Multimodal Paper Understanding Layer).

These replace the old tools/pdf/parser.py ParsedPDF/Section types. The key
upgrade is that a paper is modeled as an inventory of addressable *elements*
(figures / tables / formulas) with page numbers and bboxes, plus section text
that retains page boundaries — so "explain Figure 3 on page 7" is recoverable
and elements can be cropped, understood by the VLM, stored, and retrieved.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Section:
    """A titled body section. page_start/page_end are 1-based, 0 if unknown."""
    title: str = ""
    text: str = ""
    page_start: int = 0
    page_end: int = 0


@dataclass
class PaperElement:
    """An addressable figure / table / formula inside a paper.

    Stage 1 (structure parser) fills kind/page/section/caption/bbox/asset_path/
    image_hash/docling_extract. Stage 2 (VLM analyzers) fills `understanding`.
    """
    element_id: str = ""                       # f"{paper_id}::{kind}::{ordinal}"
    kind: str = ""                             # figure | table | formula
    ordinal: int = 0                           # per-paper, per-kind index (1-based)
    page: int = 0                              # 1-based
    section: str = ""
    caption: str = ""
    bbox: tuple[float, float, float, float] | None = None  # PDF points (x0,y0,x1,y1)
    asset_path: str | None = None              # data/assets/<paper_id>/<element_id>.png
    image_hash: str | None = None              # sha256(crop bytes); vision cache key
    docling_extract: dict = field(default_factory=dict)   # mechanical: table markdown / formula latex
    understanding: dict | None = None          # VLM semantic result (filled later)

    def short_ref(self) -> dict:
        """Lightweight reference for PaperSummary (no understanding/bbox)."""
        return {
            "element_id": self.element_id,
            "kind": self.kind,
            "page": self.page,
            "section": self.section,
            "caption": self.caption,
        }


@dataclass
class ParsedPaperDocument:
    """Full structural parse of one paper PDF."""
    sections: list[Section] = field(default_factory=list)
    raw_text: str = ""
    references: list[str] = field(default_factory=list)
    elements: list[PaperElement] = field(default_factory=list)
    is_scanned: bool = False                   # OCR was needed to recover text
    doc_fingerprint: str = ""                  # sha256(pdf bytes); cache version key
    parser_backend: str = ""                   # "docling" | "pymupdf"
    page_count: int = 0
    # Stage 1.5 observability. Digital PDFs stay ``not_needed``; scanned PDFs
    # become ``recovered`` or ``unavailable_or_empty`` after the bounded VLM
    # OCR attempt. ``ocr_chars`` counts only newly recovered text.
    ocr_status: str = "not_needed"
    ocr_chars: int = 0
