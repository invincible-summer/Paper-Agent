"""Session isolation tests for the session-scoped vector store (offline,
embedder stubbed, real ChromaDB in tmp dirs).
"""
from __future__ import annotations

import numpy as np
import pytest

import core.embeddings as emb
import tools.storage.vectorstore as vsm
from core.models import Paper, PaperSummary
from tools.storage.vectorstore import VectorStore


def _install_fake_embedder(monkeypatch, dim: int = 16):
    def _embed(texts):
        out = []
        for t in texts:
            v = np.zeros(dim)
            for ch in (t or "").lower():
                v[hash(ch) % dim] += 1.0
            n = np.linalg.norm(v)
            out.append((v / n).tolist() if n > 0 else v.tolist())
        return out

    monkeypatch.setattr(emb, "get_embedder", lambda: object())
    monkeypatch.setattr(emb, "embed_texts", _embed)
    monkeypatch.setattr(vsm, "get_embedder", lambda: object())
    monkeypatch.setattr(vsm, "embed_texts", _embed)


def _paper(pid="P1", title="Graph Neural Networks"):
    return Paper(id=pid, title=title, abstract="we study graphs", year=2023)


def test_cross_session_invisible(monkeypatch, tmp_path):
    """Papers indexed under session A must not appear in session B queries."""
    _install_fake_embedder(monkeypatch)
    vs = VectorStore(path=str(tmp_path / "chroma"))
    assert vs.upsert_paper_summary(_paper(), PaperSummary(paper_id="P1"),
                                   session_id="sessA")
    hits_a = vs.search_summaries("graph neural", session_id="sessA", top_k=5)
    hits_b = vs.search_summaries("graph neural", session_id="sessB", top_k=5)
    assert len(hits_a) == 1
    assert hits_b == []


def test_same_paper_two_sessions_no_collision(monkeypatch, tmp_path):
    """The same paper id in two sessions is stored as two separate records."""
    _install_fake_embedder(monkeypatch)
    vs = VectorStore(path=str(tmp_path / "chroma"))
    p = _paper()
    assert vs.upsert_paper_summary(p, None, session_id="sessA")
    assert vs.upsert_paper_summary(p, None, session_id="sessB")
    assert vs.count_papers() == 2
    assert vs.count_papers(session_id="sessA") == 1


def test_delete_session_scoped(monkeypatch, tmp_path):
    _install_fake_embedder(monkeypatch)
    vs = VectorStore(path=str(tmp_path / "chroma"))
    p = _paper()
    vs.upsert_paper_summary(p, None, session_id="sessA")
    vs.upsert_paper_summary(p, None, session_id="sessB")
    vs.upsert_fulltext_chunks(p, session_id="sessA", full_text="full body " * 100)
    vs.delete_session("sessA")
    assert vs.count_papers(session_id="sessA") == 0
    assert vs.count_papers(session_id="sessB") == 1
    assert vs.search_chunks("full body", session_id="sessA", top_k=3) == []


def test_chunks_scoped_by_session(monkeypatch, tmp_path):
    _install_fake_embedder(monkeypatch)
    vs = VectorStore(path=str(tmp_path / "chroma"))
    p = _paper()
    n = vs.upsert_fulltext_chunks(p, session_id="sessA",
                                  full_text="attention mechanism " * 100)
    assert n > 0
    assert vs.search_chunks("attention", session_id="sessA", top_k=3)
    assert vs.search_chunks("attention", session_id="sessB", top_k=3) == []


def test_upload_text_chunks_indexed(monkeypatch, tmp_path):
    _install_fake_embedder(monkeypatch)
    vs = VectorStore(path=str(tmp_path / "chroma"))
    n = vs.upsert_text_chunks("upload1", "myfile.txt",
                              "uploaded content about diffusion models " * 50,
                              session_id="sessA")
    assert n > 0
    hits = vs.search_chunks("diffusion models", session_id="sessA", top_k=2)
    assert hits
    assert hits[0].metadata.get("paper_id") == "upload1"


def test_fulltext_chunks_carry_parent_context(monkeypatch, tmp_path):
    """Indexed children expose parent_text/parent_id for small-to-big retrieval."""
    _install_fake_embedder(monkeypatch)
    vs = VectorStore(path=str(tmp_path / "chroma"))
    long_text = ("First sentence about graphs. " * 40
                 + "\n\n" + "Second paragraph about attention. " * 40)
    n = vs.upsert_fulltext_chunks(_paper(), session_id="sessA", full_text=long_text)
    assert n > 2  # several child chunks
    hits = vs.search_chunks("graphs", session_id="sessA", top_k=10)
    assert hits
    for h in hits:
        assert h.metadata.get("parent_text")
        assert h.metadata.get("parent_id")
        assert len(h.metadata["parent_text"]) <= 1500
    # children collapse onto fewer parents
    parent_ids = {h.metadata["parent_id"] for h in hits}
    assert len(parent_ids) < len(hits)
