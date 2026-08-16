"""Stage 1.5 — scanned-page recovery via VLM OCR.

Docling's built-in OCR (do_ocr=True, set in the structure pipeline) handles the
common scanned-PDF case at zero VLM cost. This module is the SECOND-line
fallback for the residual case where Docling's OCR still returned almost no
text (the structure parser flags this as ``is_scanned``): we render the first N
pages to PNG at 150 DPI and ask the vision model to transcribe them. VLM-OCR is
markedly more accurate than Tesseract on math/table mixed layouts, which is
exactly where a scanned academic paper is hardest.

Industrial-grade properties (no shortcuts):
- Budget-bounded: at most SCAN_VLM_PAGES_PER_PAPER pages, so a 100-page scan
  cannot spend an unbounded number of image tokens.
- Concurrency-bounded: the VisionClient's internal semaphore self-throttles;
  this module just fans out with asyncio.gather.
- Graceful no-op: returns "" when vision is unconfigured, the PDF can't be
  opened, or every page fails — the caller then degrades exactly as before
  (the old hard ``no_text`` failure), never crashing the read pipeline.
- Best-effort: per-page try/except swallows individual page failures; the pages
  that succeed still contribute their text.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

from core.multimodal import prompt_templates  # noqa: F401  (registers vision.* prompts)
from core.multimodal.vision_client import get_vision_client
from core.multimodal.cache import get_vision_cache
from core.prompts.registry import get
from core.reading_policy import SCAN_VLM_PAGES_PER_PAPER

logger = logging.getLogger(__name__)

_RENDER_DPI = 150  # whole-page render resolution fed to the VLM


async def recover_scanned_pages(pdf_path: str, *, storage_context=None) -> str:
    """VLM-OCR the first pages of a scanned PDF. Returns concatenated text.

    The caller decides whether to invoke this (typically when the structure
    parse flagged ``is_scanned``). Returns "" on any failure or when vision is
    unconfigured — callers MUST treat "" as "no recovery, degrade normally".

    Only the first ``SCAN_VLM_PAGES_PER_PAPER`` pages are transcribed: a scanned
    paper's front matter (abstract + intro + method) carries almost all of the
    answerable substance, and the budget cap keeps cost bounded for long scans.
    """
    client = get_vision_client()
    if client is None:
        # Vision unconfigured — Docling OCR already ran; nothing more we can do.
        return ""

    pdef = get("vision.ocr")
    cache = get_vision_cache(storage_context) if storage_context is not None else get_vision_cache()
    try:
        import fitz  # local import: only the rare scanned path needs PyMuPDF here
    except Exception as e:  # noqa: BLE001
        logger.debug("PyMuPDF unavailable for scanned-page recovery: %s", e)
        return ""

    try:
        fdoc = fitz.open(pdf_path)
    except Exception as e:  # noqa: BLE001
        logger.debug("scanned-page recovery open failed (%s): %s", pdf_path, e)
        return ""

    n_pages = min(fdoc.page_count, SCAN_VLM_PAGES_PER_PAPER)

    async def _ocr_one(i: int) -> str:
        try:
            page = fdoc[i]
            zoom = _RENDER_DPI / 72
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            png = pix.tobytes("png")
            kwargs = {"prompt_version": pdef.version, "expect_json": False}
            try:
                if "cache" in inspect.signature(client.analyze).parameters:
                    kwargs["cache"] = cache
            except (TypeError, ValueError):
                pass
            out = await client.analyze(png, "ocr", pdef.text, **kwargs)
            return out if isinstance(out, str) else ""
        except Exception as e:  # noqa: BLE001
            logger.debug("scanned-page OCR failed (p%d): %s", i + 1, e)
            return ""

    try:
        results = await asyncio.gather(*[_ocr_one(i) for i in range(n_pages)])
    finally:
        fdoc.close()

    return "\n\n".join(r.strip() for r in results if r and r.strip())
