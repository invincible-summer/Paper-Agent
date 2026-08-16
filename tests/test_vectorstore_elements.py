"""VectorStore elements-collection tests (offline; embeddings stubbed).

The pure helpers (_element_document, _elements_where) need no model. The
upsert/search path stubs embed_texts + get_embedder so no sentence-transformers
model is loaded — the collection mechanics mirror the well-tested chunks path.
"""

from __future__ import annotations

from types import SimpleNamespace

import tools.storage.vectorstore as vsm


def _paper(pid: str = "p1", title: str = "T") -> SimpleNamespace:
    return SimpleNamespace(id=pid, title=title, year=2024)


def _el(eid, kind, caption="", understanding=None, docling=None) -> SimpleNamespace:
    return SimpleNamespace(
        element_id=eid, kind=kind, page=1, section="Method", caption=caption,
        understanding=understanding, docling_extract=docling or {},
    )


# --- pure helpers --------------------------------------------------------------

def test_element_document_combines_sources():
    el = _el(
        "p::figure::1", "figure", caption="Architecture",
        understanding={"description": "encoder-decoder", "components": ["enc", "dec"]},
        docling={"markdown": "|m|"},
    )
    doc = vsm._element_document(el)
    assert "[Caption] Architecture" in doc
    assert "encoder-decoder" in doc
    assert "enc; dec" in doc


def test_element_document_works_without_understanding():
    el = _el("p::table::1", "table", docling={"markdown": "|a|b|"})
    assert "|a|b|" in vsm._element_document(el)


def test_elements_where_scoping():
    assert vsm._elements_where(["a", "b"]) == {"paper_id": {"$in": ["a", "b"]}}
    w = vsm._elements_where(["a"], kind="figure")
    assert w == {"$and": [{"paper_id": {"$in": ["a"]}}, {"kind": "figure"}]}


# --- upsert / search (embeddings stubbed) --------------------------------------

def _stub_embeddings(monkeypatch):
    monkeypatch.setattr(vsm, "embed_texts", lambda texts: [[0.1, 0.2, 0.3] for _ in texts])
    monkeypatch.setattr(vsm, "get_embedder", lambda: object())


def test_upsert_and_search_elements(tmp_path, monkeypatch):
    _stub_embeddings(monkeypatch)
    vs = vsm.VectorStore(path=str(tmp_path / "chroma"))
    paper = _paper()
    els = [
        _el("p1::figure::1", "figure", caption="arch",
            understanding={"description": "a transformer"}),
        _el("p1::table::1", "table", docling={"markdown": "|x|"}),
    ]
    assert vs.upsert_elements(paper, els) == 2

    hits = vs.search_elements("transformer architecture", ["p1"], top_k=5)
    assert len(hits) == 2

    # Scope by the caller's paper set preserves session isolation.
    assert vs.search_elements("anything", ["other"]) == []

    # kind filter narrows within the paper set.
    fig_hits = vs.search_elements("arch", ["p1"], kind="figure")
    assert fig_hits and all(h.metadata["kind"] == "figure" for h in fig_hits)

    # Idempotent re-upsert does not duplicate.
    vs.upsert_elements(paper, els)
    assert len(vs.search_elements("x", ["p1"], top_k=10)) == 2
