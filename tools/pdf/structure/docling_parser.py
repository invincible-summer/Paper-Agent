"""Docling-backed structure parser (primary backend).

Docling (IBM) runs a layout model that deterministically locates figures,
tables, formulas, captions, and headings, reconstructs table cell structure,
and OCRs scanned pages — replacing the heuristic regex/column detection of the
old PyMuPDF parser. This module maps DoclingDocument to our ParsedPaperDocument
and renders each element's bbox region to PNG via PyMuPDF (uniform image
production, decoupled from Docling's image-generation quirks).

Division of labor with the VLM layer (core/multimodal): Docling answers "what
elements exist and where" (structure, OCR, table cells, formula location); the
VLM answers "what each element means" (figure semantics, table interpretation,
formula LaTeX/meaning). We do not spend VLM tokens on locating tables.

The DocumentConverter is constructed lazily on first parse — it loads layout /
table-structure / OCR models (~hundreds of MB, cached under ~/.cache/docling),
so we avoid that cost when the structure layer is never used.
"""

from __future__ import annotations

import logging
from pathlib import Path

import fitz  # PyMuPDF — used only to render element crops to PNG

from core.config import get_settings
from tools.pdf.fetcher import _sanitize_filename_component as _sanitize
from tools.pdf.structure._textutils import pdf_fingerprint, split_out_references
from tools.pdf.structure.models import (
    PaperElement,
    ParsedPaperDocument,
    Section,
)

logger = logging.getLogger(__name__)

_RENDER_DPI = 150  # element crop resolution fed to the VLM


def _is_bottomleft(bbox) -> bool:
    origin = getattr(bbox, "coord_origin", None)
    name = getattr(origin, "name", str(origin))
    return "BOTTOMLEFT" in name


def _bbox_to_rect(bbox, page) -> fitz.Rect | None:
    """Convert a Docling provenance bbox to a PyMuPDF clip Rect (top-left origin)."""
    if bbox is None:
        return None
    l = getattr(bbox, "l", None)
    if l is None:
        l = getattr(bbox, "x0", 0.0)
        t = getattr(bbox, "y0", 0.0)
        r = getattr(bbox, "x1", 0.0)
        b = getattr(bbox, "y1", 0.0)
    else:
        t, r, b = bbox.t, bbox.r, bbox.b
    page_h = page.rect.height
    if _is_bottomleft(bbox):
        t, b = page_h - b, page_h - t  # flip to top-left origin
    if t > b:
        t, b = b, t
    if r < l:
        l, r = r, l
    rect = fitz.Rect(l, t, r, b)
    if rect.is_empty or rect.is_infinite or rect.width < 8 or rect.height < 8:
        return None
    return rect


class DoclingParser:
    """Primary structure backend: Docling layout + OCR + table structure."""

    backend = "docling"

    def __init__(self):
        self._converter = None

    def _ensure_converter(self):
        if self._converter is None:
            # Docling pulls RapidOCR/transformers which log verbosely at INFO.
            for noisy in ("RapidOCR", "transformers", "docling"):
                logging.getLogger(noisy).setLevel(logging.WARNING)
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import (
                DocumentConverter,
                PdfFormatOption,
            )

            # do_formula_enrichment stays off: it adds a separate formula model
            # and the VLM layer (vision.formula) produces authoritative LaTeX +
            # meaning anyway. generate_picture_images off — we render our own
            # crops from prov bboxes for uniform quality across figures/tables.
            opts = PdfPipelineOptions(
                do_ocr=True,
                do_table_structure=True,
                generate_picture_images=False,
                do_formula_enrichment=False,
            )
            self._converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=opts)
                }
            )
        return self._converter

    def parse(
        self,
        pdf_path: str,
        *,
        paper_id: str = "",
        assets_dir: str | None = None,
    ) -> ParsedPaperDocument:
        fingerprint = pdf_fingerprint(pdf_path)
        try:
            converter = self._ensure_converter()
            conv = converter.convert(pdf_path)
            doc = conv.document
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Docling conversion failed for %s (%s); returning empty parse. "
                "Caller should fall back to PyMuPDF.", pdf_path, e,
            )
            return ParsedPaperDocument(
                doc_fingerprint=fingerprint, parser_backend=self.backend
            )

        assets_root = self._assets_root(assets_dir, paper_id)
        # Open with PyMuPDF solely to render element crops to PNG.
        try:
            fdoc = fitz.open(pdf_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("PyMuPDF open failed for element rendering: %s", e)
            fdoc = None

        sections, references, elements, raw_text = self._map_document(
            doc, fdoc, paper_id, assets_root
        )
        page_count = self._page_count(doc) or (fdoc.page_count if fdoc else 0)
        is_scanned = page_count > 0 and len(raw_text) < 200
        if fdoc is not None:
            fdoc.close()

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

    # --- DoclingDocument → ParsedPaperDocument --------------------------------

    def _map_document(self, doc, fdoc, paper_id, assets_root):
        from docling_core.types.doc import DocItemLabel, PictureItem, TableItem

        heading_labels = {
            DocItemLabel.TITLE,
            DocItemLabel.SECTION_HEADER,
        }
        try:
            heading_labels.add(DocItemLabel.HEADING)
        except AttributeError:
            pass
        body_labels = {
            DocItemLabel.TEXT,
            DocItemLabel.LIST_ITEM,
            DocItemLabel.FOOTNOTE,
            DocItemLabel.PARAGRAPH,
        }
        try:
            body_labels.add(DocItemLabel.CAPTION)
        except AttributeError:
            pass  # CAPTION handled below by label name fallback

        sections: list[Section] = []
        current = Section(title="Preamble")
        references: list[str] = []
        elements: list[PaperElement] = []
        last_exhibit: PaperElement | None = None  # for caption attachment
        fig_n = tbl_n = fml_n = 0

        def close_current(page_no: int):
            if current.text.strip():
                if not current.page_end:
                    current.page_end = page_no or current.page_start
                sections.append(current)

        for entry in doc.iterate_items():
            item = entry[0] if isinstance(entry, tuple) else entry
            label = getattr(item, "label", None)
            page_no = self._page_no(item)

            # Tables ---------------------------------------------------------
            if isinstance(item, TableItem):
                tbl_n += 1
                md = self._table_markdown(item, doc)
                el = self._make_element(
                    fdoc, page_no, paper_id, "table", tbl_n, item, assets_root,
                    docling_extract={"markdown": md} if md else {},
                )
                if el is not None:
                    elements.append(el)
                    last_exhibit = el
                continue

            # Pictures (figures) --------------------------------------------
            if isinstance(item, PictureItem):
                el = self._make_element(
                    fdoc, page_no, paper_id, "figure", fig_n + 1, item, assets_root,
                )
                if el is not None:  # filtered (logo/icon) => skip cleanly
                    fig_n += 1
                    elements.append(el)
                    last_exhibit = el
                continue

            # Formula (label-based; a TextItem) ------------------------------
            label_name = getattr(label, "name", str(label))
            if label_name == "FORMULA":
                fml_n += 1
                text = (getattr(item, "text", "") or "").strip()
                el = self._make_element(
                    fdoc, page_no, paper_id, "formula", fml_n, item, assets_root,
                    docling_extract={"latex": text} if text else {},
                )
                if el is not None:
                    elements.append(el)
                    last_exhibit = el
                continue

            text = (getattr(item, "text", "") or "").strip()
            if not text:
                continue

            # Caption → attach to the most recent figure/table ----------------
            if label_name == "CAPTION" and last_exhibit is not None:
                if not last_exhibit.caption:
                    last_exhibit.caption = text
                continue

            # Heading → start a new section -----------------------------------
            if label in heading_labels or label_name in ("TITLE", "SECTION_HEADER", "HEADING"):
                close_current(page_no)
                current = Section(title=text[:200], text="", page_start=page_no or 0, page_end=page_no or 0)
                continue

            # Body text → append to current section ---------------------------
            if label in body_labels or label_name in ("TEXT", "LIST_ITEM", "FOOTNOTE", "PARAGRAPH", "CAPTION"):
                if not current.page_start:
                    current.page_start = page_no or 0
                current.text += text + "\n"
                current.page_end = page_no or current.page_end
                last_exhibit = None  # prose breaks caption association
                continue
            # else: skip (PAGE_HEADER, PAGE_FOOTER, KEY_VALUE_REGION, ...)

        close_current(0)
        sections, references = split_out_references(sections)
        raw_text = "\n\n".join(s.text.strip() for s in sections if s.text.strip())
        return sections, references, elements, raw_text

    # --- element construction --------------------------------------------------

    def _make_element(
        self, fdoc, page_no, paper_id, kind, ordinal, item, assets_root,
        *, docling_extract: dict | None = None,
    ) -> PaperElement | None:
        """Build one element. Returns None for a figure with no usable crop
        (logos/icons wrongly tagged PICTURE are filtered here by pixel size).

        Tables and formulas are kept even without a renderable crop — the table
        markdown / formula LaTeX is valuable on its own.
        """
        bbox_tuple = None
        asset_path = None
        image_hash = None
        png = None
        rect = None
        page = None
        if fdoc is not None and page_no and 1 <= page_no <= fdoc.page_count:
            page = fdoc[page_no - 1]
            prov = self._first_prov(item)
            if prov is not None:
                rect = _bbox_to_rect(getattr(prov, "bbox", None), page)
                if rect is not None:
                    bbox_tuple = (rect.x0, rect.y0, rect.x1, rect.y1)
        # Render the crop and gate by pixel size — this is what filters out
        # journal logos / author icons / decorative glyphs mislabeled PICTURE.
        if rect is not None and page is not None and assets_root is not None:
            try:
                zoom = _RENDER_DPI / 72
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect)
                if pix.width >= 100 and pix.height >= 100:
                    png = pix.tobytes("png")
            except Exception as e:  # noqa: BLE001
                logger.debug("element render failed (%s p%d): %s", kind, page_no, e)
        # A figure is only useful if we have its pixels; tables/formulas carry
        # their own structured payload and are kept regardless.
        if kind == "figure" and png is None:
            return None
        if png is not None and assets_root is not None:
            asset_path, image_hash = self._write_asset(
                assets_root, paper_id, kind, ordinal, png
            )
        element_id = f"{paper_id or 'doc'}::{kind}::{ordinal}"
        return PaperElement(
            element_id=element_id, kind=kind, ordinal=ordinal,
            page=page_no or 0, section="", caption="",
            bbox=bbox_tuple, asset_path=asset_path, image_hash=image_hash,
            docling_extract=docling_extract or {},
        )

    @staticmethod
    def _page_no(item) -> int:
        prov = DoclingParser._first_prov(item)
        if prov is None:
            return 0
        return int(getattr(prov, "page_no", 0) or 0)

    @staticmethod
    def _page_count(doc) -> int:
        """Resolve page count across Docling versions (attribute / method / pages dict)."""
        pages = getattr(doc, "pages", None)
        if isinstance(pages, (dict, list)):
            return len(pages)
        np_attr = getattr(doc, "num_pages", None)
        try:
            return int(np_attr() if callable(np_attr) else np_attr) if np_attr else 0
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _first_prov(item):
        provs = getattr(item, "prov", None)
        if provs:
            return provs[0]
        return None

    @staticmethod
    def _table_markdown(item, doc) -> str:
        try:
            md = item.export_to_markdown(doc=doc)
            return md.strip() if isinstance(md, str) else ""
        except Exception:  # noqa: BLE001
            try:
                md = item.export_to_markdown()
                return md.strip() if isinstance(md, str) else ""
            except Exception:  # noqa: BLE001
                return ""

    @staticmethod
    def _assets_root(assets_dir: str | None, paper_id: str):
        if not paper_id:
            return None
        if not assets_dir:
            assets_dir = get_settings().reader.assets_dir
        root = Path(assets_dir) / _sanitize(paper_id)
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _write_asset(assets_root, paper_id, kind, ordinal, png) -> tuple[str, str]:
        import hashlib
        name = f"{kind}_{ordinal}.png"
        path = Path(assets_root) / name
        path.write_bytes(png)
        return str(path), hashlib.sha256(png).hexdigest()
