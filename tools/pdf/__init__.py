"""Tools for acquiring and structurally parsing PDFs.

The text/element extraction entry point is the pluggable structure parser
(tools/pdf/structure): Docling primary backend with an automatic PyMuPDF
fallback. Callers use get_structure_parser() and never import a concrete
backend directly. The legacy heuristic tools.pdf.parser module has been
replaced by tools.pdf.structure.
"""

from tools.pdf.fetcher import PDFFetcher
from tools.pdf.structure import get_structure_parser

__all__ = ["PDFFetcher", "get_structure_parser"]
