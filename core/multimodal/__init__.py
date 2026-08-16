"""Multimodal (vision) layer for paper-element understanding.

An independent OpenAI-compatible vision client (MULTIMODAL_* config) used only
for figures / tables / formulas / scanned-page OCR. The text LLM (DeepSeek via
core/llm.py) is never touched. When vision is unconfigured or the provider
fails, every entry point degrades gracefully to text-only behavior — vision is
an enhancement, never a hard dependency.

Submodules:
- vision_client:   VisionClient + get_vision_client() singleton.
- cache:           image-hash result cache (memory LRU + SQLite).
- prompt_templates: figure/table/formula/ocr prompts (prompt registry).
- analyzers:       Stage 2 orchestration — understand_elements() fans out the
                   vision client over a paper's figures/tables/formulas with
                   partial-failure tolerance, budget caps, and cache reuse.
- ocr:             Stage 1.5 — recover_scanned_pages() VLM-OCRs the pages
                   Docling's OCR could not (the residual scanned-page case).
"""

from core.multimodal.analyzers import (
    analyze_figure,
    analyze_formula,
    analyze_table,
    understand_elements,
)
from core.multimodal.ocr import recover_scanned_pages
from core.multimodal.vision_client import VisionClient, get_vision_client

__all__ = [
    "VisionClient",
    "get_vision_client",
    "understand_elements",
    "analyze_figure",
    "analyze_table",
    "analyze_formula",
    "recover_scanned_pages",
]
