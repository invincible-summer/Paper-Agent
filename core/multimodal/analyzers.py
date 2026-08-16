"""Stage 2 — VLM semantic understanding of paper elements (figures/tables/formulas).

This is the bridge between the deterministic structure layer (tools/pdf/structure,
which answers "what elements exist and where") and the storage/retrieval layer
(which needs "what each element means"). For every addressable element we ask
the vision model (core/multimodal/vision_client, MULTIMODAL_* endpoint) for a
structured semantic reading and store it on `element.understanding`.

Industrial-grade properties (no shortcuts):
- Partial-failure tolerant: each element is understood inside its own try/except.
  One figure timing out, one table returning garbage, or the breaker being open
  never blocks the other elements or the main text extraction. Elements that
  fail simply keep their Docling extract + caption and are still indexed.
- Cost-bounded: figures > tables > formulas prioritization, then a hard
  ELEMENTS_PER_PAPER_CAP and a hard VISION_CALLS_PER_PAPER cap. Cache hits are
  free and never count against the call budget — re-reading a paper costs zero
  VLM tokens (the cache is keyed by image hash + task + prompt version).
- Concurrency-bounded by the VisionClient's own internal semaphore; this module
  just fans out with asyncio.gather and the client self-throttles.
- Graceful no-op: when vision is unconfigured (get_vision_client() is None) the
  function returns the elements unchanged. Vision is an enhancement, never a
  hard dependency.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path

from core.multimodal import prompt_templates  # noqa: F401  (registers vision.* prompts)
from core.multimodal.cache import get_vision_cache
from core.multimodal.vision_client import get_vision_client
from core.prompts.registry import get
from core.reading_policy import ELEMENTS_PER_PAPER_CAP, VISION_CALLS_PER_PAPER

logger = logging.getLogger(__name__)

# Figures are the most semantically rich and the hardest to recover from text
# alone, so when a budget cap binds they win over tables, then formulas.
_KIND_PRIORITY = {"figure": 0, "table": 1, "formula": 2}

# Lazily-built kind -> (cache-bucket task, registered PromptDef) dispatch. Built
# lazily so the registry is guaranteed populated regardless of import order.
_DISPATCH: dict[str, tuple[str, object]] | None = None


def _kind_dispatch() -> dict[str, tuple[str, object]]:
    global _DISPATCH
    if _DISPATCH is None:
        _DISPATCH = {
            "figure": ("figure", get("vision.figure")),
            "table": ("table", get("vision.table")),
            "formula": ("formula", get("vision.formula")),
        }
    return _DISPATCH


def _prioritize(elements) -> list:
    """Eligible elements sorted figures-first, then tables, then formulas,
    each in document order (ordinal). Elements of unknown kinds are dropped."""
    eligible = [el for el in elements if getattr(el, "kind", "") in _KIND_PRIORITY]
    eligible.sort(key=lambda el: (_KIND_PRIORITY[el.kind], el.ordinal))
    return eligible


def _peek_cache(cache, image_hash: str | None, task: str, version: int) -> dict | None:
    """Return a cached understanding dict (free) or None. Uses the element's
    stored image_hash, which equals sha256(crop bytes) — the same key the
    VisionClient hashes internally, so a hit here is exactly a hit there."""
    if not image_hash:
        return None
    val = cache.get(image_hash, task, version)
    return val if isinstance(val, dict) else None


def _apply_result(el, result: dict) -> None:
    """Merge one VLM result onto an element, with kind-specific rules.

    figure:  understanding = full VLM dict {type, description, components,
              relations, role_in_paper}.
    table:   understanding = full VLM dict; Docling's TableFormer markdown is
              structurally reliable, so it is kept and the VLM markdown only
              backfills the extract when Docling produced none.
    formula: understanding = full VLM dict {latex, meaning, variables, role};
              the VLM's LaTeX is authoritative, so it upgrades the extract's
              initial LaTeX (the [Formula] embed line then carries the corrected
              source).
    """
    el.understanding = result
    if el.kind == "table":
        if not (el.docling_extract or {}).get("markdown") and (result.get("markdown") or "").strip():
            el.docling_extract = {**(el.docling_extract or {}), "markdown": result["markdown"]}
    elif el.kind == "formula":
        vlm_latex = (result.get("latex") or "").strip()
        if vlm_latex and vlm_latex != (el.docling_extract or {}).get("latex"):
            el.docling_extract = {**(el.docling_extract or {}), "latex": vlm_latex}


async def _analyze_one(client, el, cache=None) -> None:
    """Understand one element via the VLM. Never raises — failures are logged
    and the element is left with whatever Docling extract it already had."""
    task, pdef = _kind_dispatch()[el.kind]
    try:
        png = Path(el.asset_path).read_bytes()
    except Exception as e:  # noqa: BLE001
        logger.debug("asset read failed for %s: %s", el.element_id, e)
        return
    try:
        suffix = Path(el.asset_path).suffix.lower()
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
            suffix, "image/png"
        )
        result = await client.analyze(
            png, task, pdef.text,
            prompt_version=pdef.version, expect_json=True, mime=mime,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("vision analyze failed for %s: %s", el.element_id, e)
        return
    if isinstance(result, dict):
        _apply_result(el, result)


async def understand_elements(elements, *, focus: str = "", storage_context=None) -> list:
    """Stage 2 entry: VLM-understand a paper's elements in a bounded async pool.

    Mutates elements in place (fills `.understanding`) and returns the same
    list. Safe to call with an empty list, with vision unconfigured, or with a
    breaker open — every path degrades to "elements keep their Docling extract".

    `focus` is reserved for future caption-relevance prioritization; today
    prioritization is figure > table > formula (deterministic, testable).
    """
    if not elements:
        return elements

    candidates = _prioritize(elements)[:ELEMENTS_PER_PAPER_CAP]

    client = get_vision_client()
    if client is None:
        # Vision unconfigured — nothing to do; elements keep docling_extract.
        return elements

    cache = get_vision_cache(storage_context) if storage_context is not None else get_vision_cache()
    dispatch = _kind_dispatch()
    billable: list = []
    for el in candidates:
        task, pdef = dispatch[el.kind]
        cached = _peek_cache(cache, el.image_hash, task, pdef.version)
        if cached is not None:
            _apply_result(el, cached)          # free — never counts vs budget
        elif getattr(el, "asset_path", None):
            billable.append(el)                 # needs a real VLM call
        # else: table/formula with no rendered crop — keep Docling extract only.

    # Hard cap on billable calls per paper (cache hits are already excluded).
    billable = billable[:VISION_CALLS_PER_PAPER]
    if not billable:
        return elements

    # Fan out; the VisionClient bounds concurrency via its internal semaphore,
    # and each _analyze_one swallows its own failures (partial-failure tolerance).
    await asyncio.gather(*[_analyze_one(client, el, cache) for el in billable])
    return elements


# --- per-kind entry points (thin wrappers for clarity / direct unit testing) ----

async def analyze_figure(client, el) -> None:
    """Understand a single figure element."""
    await _analyze_one(client, el)


async def analyze_table(client, el) -> None:
    """Understand a single table element (Docling markdown preferred)."""
    await _analyze_one(client, el)


async def analyze_formula(client, el) -> None:
    """Understand a single formula element (VLM LaTeX authoritative)."""
    await _analyze_one(client, el)
