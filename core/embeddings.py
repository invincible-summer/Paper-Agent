"""Phase 2 RAG: local sentence-transformers embedder (DESIGN D-005).

Lazy singleton that loads a bilingual MiniLM model on first use. Returns None on
any failure so callers can degrade gracefully (the main pipeline never breaks
because embeddings are unavailable). Zero API keys -- the model runs on CPU.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
from core.config import get_settings

_MODEL = None
_TRIED = False


def get_embedder():
    """Return a lazy SentenceTransformer singleton, or None on failure.

    The model (~120MB) downloads to ~/.cache on first use. If loading fails for
    any reason, every subsequent call returns None without retrying, so callers
    skip vectorization and the main pipeline keeps working. Per D-005 no online
    embedding API is used; the API key never leaves this machine.
    """
    global _MODEL, _TRIED
    if _TRIED:
        return _MODEL
    _TRIED = True
    try:
        from sentence_transformers import SentenceTransformer

        name = get_settings().storage.embedding_model
        logger.info("Loading embedding model %s ...", name)
        _MODEL = SentenceTransformer(name)
        logger.info("Embedding model ready (dim=%d).", _MODEL.get_embedding_dimension() if hasattr(_MODEL, "get_embedding_dimension") else _MODEL.get_sentence_embedding_dimension())
    except Exception as e:  # noqa: BLE001 - any failure is non-fatal here
        logger.warning("Embedding model unavailable, RAG will be disabled: %s", e)
        _MODEL = None
    return _MODEL


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of texts. Returns None if the model is unavailable.

    Empty input returns an empty list. Vectors are L2-normalized for cosine
    similarity, so callers compare with a dot product.
    """
    if not texts:
        return []
    model = get_embedder()
    if model is None:
        return None
    try:
        import numpy as np

        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [np.asarray(v).tolist() for v in vecs]
    except Exception as e:  # noqa: BLE001
        logger.warning("Embedding failed: %s", e)
        return None


def embedding_dim() -> int | None:
    """Return the model's vector dimension, or None if unavailable."""
    model = get_embedder()
    if model is None:
        return None
    try:
        return model.get_embedding_dimension() if hasattr(model, "get_embedding_dimension") else model.get_sentence_embedding_dimension()
    except Exception:  # noqa: BLE001
        return None


def reset_embedder() -> None:
    """Clear the cached model state (tests / config changes)."""
    global _MODEL, _TRIED
    _MODEL = None
    _TRIED = False
