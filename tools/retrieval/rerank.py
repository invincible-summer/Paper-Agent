"""Cross-encoder reranking (bge-reranker-base) with graceful degradation.

Lazy singleton mirroring core/embeddings.py: the model downloads to ~/.cache
on first use; any load failure (or an empty `storage.reranker_model` setting)
disables reranking permanently for the process and callers keep the RRF order.
Runs on CPU via sentence-transformers — no new dependency, no API key.
"""
from __future__ import annotations

import logging

from core.config import get_settings

logger = logging.getLogger(__name__)

_MODEL = None
_TRIED = False


def get_reranker():
    """Return a lazy CrossEncoder singleton, or None when disabled/unavailable."""
    global _MODEL, _TRIED
    if _TRIED:
        return _MODEL
    _TRIED = True
    name = (get_settings().storage.reranker_model or "").strip()
    if not name:
        logger.info("Reranker disabled (storage.reranker_model is empty).")
        return None
    try:
        from sentence_transformers import CrossEncoder

        logger.info("Loading reranker model %s ...", name)
        _MODEL = CrossEncoder(name)
        logger.info("Reranker model ready.")
    except Exception as e:  # noqa: BLE001 - any failure is non-fatal here
        logger.warning("Reranker unavailable, keeping RRF order: %s", e)
        _MODEL = None
    return _MODEL


def rerank(query: str, passages: list[str], top_k: int | None = None) -> list[int]:
    """Return passage indices ordered by cross-encoder relevance.

    Falls back to the original order when the model is unavailable or scoring
    fails, so the RRF order is preserved end-to-end.
    """
    n = len(passages)
    order = list(range(n))
    model = get_reranker()
    if model is not None and n > 1 and query:
        try:
            scores = model.predict([(query, p) for p in passages])
            order = sorted(range(n), key=lambda i: float(scores[i]), reverse=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("Rerank scoring failed, keeping RRF order: %s", e)
    if top_k is not None:
        order = order[:max(top_k, 0)]
    return order


def reset_reranker() -> None:
    """Clear the cached model state (tests / config changes)."""
    global _MODEL, _TRIED
    _MODEL = None
    _TRIED = False
