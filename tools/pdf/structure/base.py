"""Structure parser interface + factory (Multimodal Paper Understanding Layer).

The structure layer is pluggable: a primary Docling backend (layout model +
OCR + table structure) with an automatic PyMuPDF fallback when Docling is
unavailable or misconfigured. Callers use get_structure_parser() and never
import a concrete backend directly.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from core.config import get_settings
from tools.pdf.structure.models import ParsedPaperDocument

logger = logging.getLogger(__name__)


@runtime_checkable
class PaperStructureParser(Protocol):
    """Parse a PDF into a ParsedPaperDocument (sections + elements + text).

    Implementations must accept paper_id + assets_dir kwargs so element asset
    paths and ids can be named deterministically.
    """
    backend: str

    def parse(
        self,
        pdf_path: str,
        *,
        paper_id: str = "",
        assets_dir: str | None = None,
    ) -> ParsedPaperDocument: ...


_PARSER: PaperStructureParser | None = None


def get_structure_parser() -> PaperStructureParser:
    """Return the configured structure parser, auto-falling back to PyMuPDF.

    The choice is cached for the process (mirrors get_llm). If Docling is
    requested but fails to import/construct, we log once and use PyMuPDF so the
    read pipeline always has a working parser.
    """
    global _PARSER
    if _PARSER is not None:
        return _PARSER

    backend = get_settings().reader.structure_backend
    if backend == "docling":
        try:
            from tools.pdf.structure.docling_parser import DoclingParser

            _PARSER = DoclingParser()
            logger.info("structure backend: docling")
            return _PARSER
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Docling backend unavailable (%s); falling back to PyMuPDF "
                "structure parser.", e,
            )

    from tools.pdf.structure.pymupdf_parser import PyMuPDFStructureParser

    _PARSER = PyMuPDFStructureParser()
    logger.info("structure backend: pymupdf")
    return _PARSER
