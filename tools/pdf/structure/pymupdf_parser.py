"""PyMuPDF structure parser (fallback backend).

Used when Docling is unavailable or misconfigured. Migrates the original
tools/pdf/parser.py logic (column-aware text extraction, regex section split,
reference merging) with two upgrades the old parser lacked:

- page numbers are preserved per section (the old parser flattened pages, so
  "Figure 3 on page 7" was unrecoverable),
- repeating running headers/footers are detected across pages and stripped
  (the old parser docstring claimed this but the code never did it).

Figure/table element extraction is best-effort here (embedded images via
page.get_images, table cells via page.find_tables). Formula detection is left
to the Docling backend.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

import fitz  # PyMuPDF

from core.config import get_settings
from tools.pdf.fetcher import _sanitize_filename_component as _sanitize
from tools.pdf.structure._textutils import (
    pdf_fingerprint,
    split_out_references,
)
from tools.pdf.structure.models import (
    PaperElement,
    ParsedPaperDocument,
    Section,
)

logger = logging.getLogger(__name__)

SECTION_PATTERNS = re.compile(
    r"^(?:\d+\.?\s*)?(abstract|introduction|background|related\s+work|method"
    r"|methodology|approach|experiment|evaluation|result|discussion"
    r"|conclusion|acknowledgment|reference|appendix|supplementary)",
    re.IGNORECASE,
)
_CAP_START_RE = re.compile(
    r"^(figure|fig\.|table|tab\.|图|表)\s*\.?\s*(\d+)", re.IGNORECASE
)

# Render DPI for element crops fed to the VLM.
_RENDER_DPI = 150


class PyMuPDFStructureParser:
    """Column-aware text + best-effort element extraction (fallback backend)."""

    backend = "pymupdf"

    def parse(
        self,
        pdf_path: str,
        *,
        paper_id: str = "",
        assets_dir: str | None = None,
    ) -> ParsedPaperDocument:
        fingerprint = pdf_fingerprint(pdf_path)
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to open PDF %s: %s", pdf_path, e)
            return ParsedPaperDocument(
                doc_fingerprint=fingerprint, parser_backend=self.backend
            )

        page_count = doc.page_count
        page_texts = self._extract_pages_clean(doc)
        sections, references, raw_text = self._build_sections(page_texts)
        elements = self._extract_elements(doc, paper_id, assets_dir)
        is_scanned = page_count > 0 and len(raw_text) < 200
        doc.close()

        return ParsedPaperDocument(
            sections=sections,
            raw_text=raw_text,
            references=references,
            elements=elements,
            is_scanned=is_scanned,
            doc_fingerprint=fingerprint,
            parser_backend=self.backend,
            page_count=page_count,
        )

    # --- text extraction ------------------------------------------------------

    def _extract_pages_clean(self, doc) -> list[str]:
        """Per-page column-aware text with running headers/footers removed."""
        page_texts: list[str] = []
        tops: list[str] = []
        bottoms: list[str] = []
        for page in doc:
            text = self._extract_page_sorted(page)
            page_texts.append(text)
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            if lines:
                tops.append(lines[0])
            if len(lines) > 1:
                bottoms.append(lines[-1])

        n = max(doc.page_count, 1)
        threshold = max(2, n // 2)  # a real running header repeats on >= half the pages
        headers = {ln for ln, c in Counter(tops).items() if c >= threshold and len(ln) < 120}
        footers = {ln for ln, c in Counter(bottoms).items() if c >= threshold and len(ln) < 120}
        if headers or footers:
            drop = headers | footers
            page_texts = [
                "\n".join(ln for ln in t.split("\n") if ln.strip() not in drop)
                for t in page_texts
            ]
        return page_texts

    def _extract_page_sorted(self, page) -> str:
        """Column-aware text extraction (DESIGN D-013)."""
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        text_blocks = [b for b in blocks if b["type"] == 0 and b.get("lines")]
        if not text_blocks:
            return ""
        page_width = page.rect.width
        if self._detect_two_column(text_blocks, page_width):
            mid_x = page_width / 2
            left = [b for b in text_blocks if (b["bbox"][0] + b["bbox"][2]) / 2 < mid_x]
            right = [b for b in text_blocks if (b["bbox"][0] + b["bbox"][2]) / 2 >= mid_x]
            sorted_blocks = self._sort_by_y(left) + self._sort_by_y(right)
        else:
            sorted_blocks = self._sort_by_y(text_blocks)
        return "\n".join(self._extract_block_text(b) for b in sorted_blocks)

    def _detect_two_column(self, blocks: list[dict], page_width: float) -> bool:
        centers = [(b["bbox"][0] + b["bbox"][2]) / 2 for b in blocks]
        if len(centers) < 4:
            return False
        mid = page_width / 2
        total = len(centers)
        left = sum(1 for c in centers if c < mid)
        right = total - left
        return left > total * 0.2 and right > total * 0.2

    def _sort_by_y(self, blocks: list[dict]) -> list[dict]:
        return sorted(blocks, key=lambda b: b["bbox"][1])

    def _extract_block_text(self, block: dict) -> str:
        lines = []
        for line in block.get("lines", []):
            spans = [span["text"] for span in line.get("spans", [])]
            lines.append("".join(spans))
        return "\n".join(lines).strip()

    # --- sections -------------------------------------------------------------

    def _build_sections(
        self, page_texts: list[str]
    ) -> tuple[list[Section], list[str], str]:
        """Split per-page text into sections that remember their page range."""
        sections: list[Section] = []
        current = Section(title="Preamble")
        for page_idx, text in enumerate(page_texts, start=1):
            for line in text.split("\n"):
                stripped = line.strip()
                if not stripped:
                    if current.text:
                        current.text += "\n"
                    continue
                if SECTION_PATTERNS.match(stripped) and len(stripped) < 80:
                    if current.text.strip():
                        self._close_section(sections, current, page_idx)
                    current = Section(title=stripped, page_start=page_idx, page_end=page_idx)
                else:
                    if not current.text:
                        current.page_start = page_idx
                    current.text += line + "\n"
                    current.page_end = page_idx
        if current.text.strip():
            self._close_section(sections, current, len(page_texts))
        sections, references = split_out_references(sections)
        raw_text = "\n\n".join(s.text.strip() for s in sections if s.text.strip())
        return sections, references, raw_text

    @staticmethod
    def _close_section(sections: list[Section], sec: Section, page_idx: int = 0) -> None:
        if not sec.page_end:
            sec.page_end = page_idx or sec.page_start
        sections.append(sec)

    # --- elements (figures + tables) ------------------------------------------

    def _extract_elements(
        self, doc, paper_id: str, assets_dir: str | None
    ) -> list[PaperElement]:
        assets_root = self._assets_root(assets_dir, paper_id)
        out: list[PaperElement] = []
        fig_n = tbl_n = 0
        for pidx, page in enumerate(doc, start=1):
            # Figures: embedded raster images with their placement rect.
            seen_rects: set = set()
            for img in page.get_images(full=True):
                xref = img[0]
                for rect in page.get_image_rects(xref):
                    key = (round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))
                    if key in seen_rects or rect.width < 40 or rect.height < 40:
                        continue
                    seen_rects.add(key)
                    fig_n += 1
                    out.append(self._make_element(
                        doc, page, pidx, paper_id, "figure", fig_n, rect, assets_root,
                        caption=self._find_caption(page, "figure", fig_n),
                    ))
            # Tables: PyMuPDF's built-in table finder (>=1.23).
            try:
                finder = page.find_tables()
                for t in finder.tables:
                    tbl_n += 1
                    rect = fitz.Rect(t.bbox) if t.bbox else None
                    md = ""
                    try:
                        extracted = t.extract()
                        md = _rows_to_markdown(extracted) if extracted else ""
                    except Exception:  # noqa: BLE001
                        md = ""
                    out.append(self._make_element(
                        doc, page, pidx, paper_id, "table", tbl_n, rect, assets_root,
                        caption=self._find_caption(page, "table", tbl_n),
                        docling_extract={"markdown": md} if md else {},
                    ))
            except Exception as e:  # noqa: BLE001
                logger.debug("find_tables failed on page %d: %s", pidx, e)
        return out

    def _make_element(
        self, doc, page, page_idx, paper_id, kind, ordinal, rect, assets_root,
        *, caption: str = "", docling_extract: dict | None = None,
    ) -> PaperElement:
        bbox = (rect.x0, rect.y0, rect.x1, rect.y1) if rect else None
        element_id = f"{paper_id or 'doc'}::{kind}::{ordinal}"
        asset_path = None
        image_hash = None
        if rect is not None and assets_root is not None:
            try:
                zoom = _RENDER_DPI / 72
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect)
                png = pix.tobytes("png")
                asset_path, image_hash = self._write_asset(
                    assets_root, element_id, png
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("element render failed (%s p%d): %s", kind, page_idx, e)
        return PaperElement(
            element_id=element_id, kind=kind, ordinal=ordinal, page=page_idx,
            section="", caption=caption or "", bbox=bbox, asset_path=asset_path,
            image_hash=image_hash, docling_extract=docling_extract or {},
        )

    def _find_caption(self, page, kind: str, n: int) -> str:
        """Best-effort: find a 'Figure N' / 'Table N' caption line on the page."""
        text = page.get_text("text")
        for line in text.split("\n"):
            m = _CAP_START_RE.match(line.strip())
            if m and int(m.group(2)) == n:
                return line.strip()
        return ""

    @staticmethod
    def _assets_root(assets_dir: str | None, paper_id: str):
        if not assets_dir:
            assets_dir = get_settings().reader.assets_dir
        if not paper_id:
            return None  # no paper_id => don't write assets
        from pathlib import Path
        root = Path(assets_dir) / _sanitize(paper_id)
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _write_asset(assets_root, element_id: str, png: bytes) -> tuple[str, str]:
        import hashlib
        from pathlib import Path
        path = Path(assets_root) / f"{_sanitize(element_id)}.png"
        path.write_bytes(png)
        return str(path), hashlib.sha256(png).hexdigest()


def _rows_to_markdown(rows: list) -> str:
    """Render PyMuPDF table rows (list of lists) as a Markdown table."""
    if not rows:
        return ""
    header = rows[0]
    body = rows[1:] if len(rows) > 1 else []
    def esc(c):
        return ("" if c is None else str(c)).replace("|", "\\|").replace("\n", " ").strip()
    out = ["| " + " | ".join(esc(c) for c in header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    for r in body:
        # pad/trim to header width
        cells = list(r) + [""] * (len(header) - len(r))
        out.append("| " + " | ".join(esc(c) for c in cells[:len(header)]) + " |")
    return "\n".join(out)
