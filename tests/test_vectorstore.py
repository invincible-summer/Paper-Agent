"""Phase 2 RAG: ChromaDB vector store tests (offline; embedder stubbed).

Uses a real PersistentClient in a tmp dir but injects deterministic embeddings
so no model download is needed. Covers the two collections, idempotent upsert,
and filtered retrieval (by paper_id).
"""

from __future__ import annotations

import core.embeddings as emb
from core.models import Paper, PaperSummary
import tools.storage.vectorstore as vsm
from tools.pdf.structure.models import Section
from tools.storage.vectorstore import (
    VectorStore,
    SearchHit,
    build_fulltext_chunks,
    _summary_document,
)


def _install_fake_embedder(monkeypatch, dim: int = 16):
    """Make embed_texts return a deterministic vector per distinct text.

    Similar texts (sharing tokens) map to nearby vectors so cosine retrieval is
    meaningful; very different texts map to orthogonal vectors.
    """
    def _embed(texts):
        import numpy as np

        out = []
        for t in texts:
            low = (t or "").lower()
            v = np.zeros(dim, dtype=float)
            # Bucket each char into a dim slot so overlapping text overlaps.
            for ch in low:
                v[hash(ch) % dim] += 1.0
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
            out.append(v.tolist())
        return out

    monkeypatch.setattr(emb, "get_embedder", lambda: object())  # truthy probe
    monkeypatch.setattr(emb, "embed_texts", _embed)
    # vectorstore imported embed_texts by name; patch its module binding too.
    monkeypatch.setattr(vsm, "get_embedder", lambda: object())
    monkeypatch.setattr(vsm, "embed_texts", _embed)


def _paper(pid="P1", title="Graph Neural Networks", abstract="We study GNNs."):
    return Paper(id=pid, title=title, abstract=abstract, year=2023)


def test_build_fulltext_chunks_parent_child_structure():
    """Children are small chunks; each carries its parent passage + parent_id."""
    sections = [Section("Method", "novel attention mechanism. " * 60)]
    records = build_fulltext_chunks(sections, None)
    assert len(records) > 1
    for r in records:
        assert r["parent_text"]
        assert r["parent_id"]
        assert len(r["parent_text"]) <= 1500
    # children of the same parent share its id; the parent holds full context
    parent_ids = {r["parent_id"] for r in records}
    assert len(parent_ids) < len(records)
    for r in records:
        assert len(r["parent_text"]) >= len(r["text"].strip()) - 100  # overlap prefix slack


def test_build_fulltext_chunks_from_sections():
    sections = [Section("Method", "method body " * 200), Section("Results", "results here")]
    records = build_fulltext_chunks(sections, None)
    assert len(records) >= 2
    assert {r["section"] for r in records} >= {"Method", "Results"}
    # long section gets split into multiple chunks
    method_chunks = [r for r in records if r["section"] == "Method"]
    assert len(method_chunks) > 1


def test_build_fulltext_chunks_fallback_to_raw():
    records = build_fulltext_chunks([], "raw full text body " * 50)
    assert records
    assert all(r["section"] == "full" for r in records)


def test_summary_document_combines_fields():
    p = _paper()
    s = PaperSummary(paper_id="P1", research_problem="the problem", key_findings=["f1", "f2"])
    doc = _summary_document(s, p)
    assert "Graph Neural Networks" in doc
    assert "We study GNNs." in doc
    assert "the problem" in doc


def test_upsert_and_search_summaries(monkeypatch, tmp_path):
    _install_fake_embedder(monkeypatch)
    vs = VectorStore(path=str(tmp_path / "chroma"))
    p1 = _paper("P1", "Transformer attention", "Attention is all you need.")
    p2 = _paper("P2", "Reinforcement learning", "Reward maximization agents.")
    s1 = PaperSummary(paper_id="P1")
    s2 = PaperSummary(paper_id="P2")

    assert vs.upsert_paper_summary(p1, s1)
    assert vs.upsert_paper_summary(p2, s2)
    assert vs.count_papers() == 2

    hits = vs.search_summaries("attention transformer", top_k=2)
    assert hits
    assert any(h.metadata.get("paper_id") == "P1" for h in hits)


def test_search_chunks_filter_by_paper(monkeypatch, tmp_path):
    _install_fake_embedder(monkeypatch)
    vs = VectorStore(path=str(tmp_path / "chroma"))
    p1 = _paper("P1", "Methods paper", "intro")
    sections = [Section("Method", "novel attention mechanism " * 40)]
    n = vs.upsert_fulltext_chunks(p1, parsed_sections=sections, full_text=None)
    assert n > 0

    hits = vs.search_chunks("attention mechanism", paper_id="P1", top_k=3)
    assert hits
    assert all(h.metadata.get("paper_id") == "P1" for h in hits)
    # filtering to a nonexistent paper yields nothing
    none_hits = vs.search_chunks("attention", paper_id="NOPE", top_k=3)
    assert none_hits == []


def test_idempotent_re_upsert(monkeypatch, tmp_path):
    _install_fake_embedder(monkeypatch)
    vs = VectorStore(path=str(tmp_path / "chroma"))
    p = _paper("P1", "Dup paper", "body")
    s = PaperSummary(paper_id="P1")
    vs.upsert_paper_summary(p, s)
    vs.upsert_paper_summary(p, s)
    assert vs.count_papers() == 1


def test_unavailable_store_returns_empty(monkeypatch, tmp_path):
    """When no embedder can load, all ops degrade gracefully."""
    monkeypatch.setattr(emb, "get_embedder", lambda: None)
    monkeypatch.setattr(vsm, "get_embedder", lambda: None)
    vs = VectorStore(path=str(tmp_path / "chroma"))
    assert vs.available() is False
    assert vs.search_summaries("x") == []
    assert vs.upsert_paper_summary(_paper(), PaperSummary()) is False
    assert vs.count_papers() == 0
