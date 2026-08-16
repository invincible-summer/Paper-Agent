"""Phase 2 RAG: embedder tests (offline; model load is lazy + best-effort).

The singleton degrades to None if sentence-transformers is unavailable, so these
tests never require a real model download. They assert the public contract:
get_embedder() is stable and cached; embed_texts returns None / [] gracefully;
reset_embedder clears state.
"""

from __future__ import annotations

import pytest

import core.embeddings as emb




@pytest.mark.slow
def test_get_embedder_is_cached_singleton():
    """Repeated calls return the same object (or both None) without retrying."""
    a = emb.get_embedder()
    b = emb.get_embedder()
    assert a is b


def test_embed_texts_empty_returns_empty():
    """Empty input -> [] regardless of model availability."""
    assert emb.embed_texts([]) == []


def test_embed_texts_none_when_no_model(monkeypatch):
    """Without a model, embed_texts returns None (graceful degradation)."""
    emb.reset_embedder()
    monkeypatch.setattr(emb, "get_embedder", lambda: None)
    assert emb.embed_texts(["some text"]) is None


def test_embed_texts_uses_model(monkeypatch):
    """When a model is present, embed_texts forwards to model.encode normalized."""

    class _FakeModel:
        dim = 384

        def get_sentence_embedding_dimension(self):
            return self.dim

        def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
            # Return one fixed-size vector per input text.
            import numpy as np

            return [np.ones(self.dim) for _ in texts]

    emb.reset_embedder()
    monkeypatch.setattr(emb, "get_embedder", lambda: _FakeModel())
    vecs = emb.embed_texts(["a", "bb"])
    assert vecs is not None
    assert len(vecs) == 2
    assert len(vecs[0]) == 384


def test_embedding_dim_matches_model(monkeypatch):
    class _FakeModel:
        def get_sentence_embedding_dimension(self):
            return 256

    emb.reset_embedder()
    monkeypatch.setattr(emb, "get_embedder", lambda: _FakeModel())
    assert emb.embedding_dim() == 256


def test_embedding_dim_none_without_model(monkeypatch):
    emb.reset_embedder()
    monkeypatch.setattr(emb, "get_embedder", lambda: None)
    assert emb.embedding_dim() is None


@pytest.mark.slow
def test_reset_clears_cache():
    """reset_embedder forces a fresh load attempt on next call."""
    emb.get_embedder()  # populate cache
    emb.reset_embedder()
    # After reset, _TRIED must be False so the next call re-attempts.
    assert emb._TRIED is False
