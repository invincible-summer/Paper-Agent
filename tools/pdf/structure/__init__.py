"""Paper structure parsing (Multimodal Paper Understanding Layer, Stage 1).

Pluggable backends behind the PaperStructureParser protocol:
- DoclingParser (primary): layout model + OCR + table structure + formula regions.
- PyMuPDFStructureParser (fallback): column-aware text + regex sections.

Use get_structure_parser() to obtain the configured backend; it auto-falls
back to PyMuPDF if Docling is unavailable. ParsedPaperDocument carries section
text with page numbers plus an inventory of addressable PaperElement objects
(figures / tables / formulas) that Stage 2 (core/multimodal) understands.
"""

from tools.pdf.structure.base import PaperStructureParser, get_structure_parser
from tools.pdf.structure.models import PaperElement, ParsedPaperDocument, Section

__all__ = [
    "PaperStructureParser",
    "get_structure_parser",
    "ParsedPaperDocument",
    "PaperElement",
    "Section",
]
