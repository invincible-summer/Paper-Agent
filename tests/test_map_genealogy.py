"""Tests for genealogy graph data + research map / reading path helpers."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from agents.map_agent import (
    build_timeline,
    cluster_papers,
    pick_reading_order,
)
from core.models import Paper
from tools.storage import genealogy as genealogy_mod
from tools.storage.genealogy import (
    build_genealogy,
    compute_roles,
    compute_semantic_edges,
)


def _fake_embed(texts):
    """Word-hash embeddings: texts sharing words are close, others ~orthogonal."""
    out = []
    for t in texts:
        v = np.zeros(32)
        for w in (t or "").lower().split():
            v[hash(w) % 32] += 1.0
        n = np.linalg.norm(v)
        out.append((v / n).tolist() if n > 0 else v.tolist())
    return out


def _papers(n=5):
    return [Paper(id=f"P{i}", title=f"paper {i}", year=2018 + i,
                  citation_count=(n - i) * 10, abstract=f"abstract {i}")
            for i in range(n)]


# --- genealogy ---------------------------------------------------------------

def test_semantic_edges_skip_existing_pairs():
    a = Paper(id="A", title="graph neural networks", abstract="graph neural nets")
    b = Paper(id="B", title="graph neural networks", abstract="graph neural nets")
    edges = compute_semantic_edges([a, b], existing_pairs={("A", "B")},
                                   embed_fn=_fake_embed)
    assert edges == []


def test_semantic_edges_threshold():
    a = Paper(id="A", title="graph neural networks", abstract="graph neural nets")
    b = Paper(id="B", title="graph neural networks", abstract="graph neural nets")
    c = Paper(id="C", title="medieval castles", abstract="knights and stone")
    edges = compute_semantic_edges([a, b, c], existing_pairs=set(),
                                   embed_fn=_fake_embed)
    pairs = {(e["source"], e["target"]) for e in edges}
    assert ("A", "B") in pairs
    assert all("C" not in p for p in pairs)


def test_semantic_edges_no_embedder():
    papers = _papers(3)
    assert compute_semantic_edges(papers, set(), embed_fn=None) == []


def test_roles_citation_pagerank():
    papers = _papers(5)
    # P4 (oldest, most cited) receives citation edges from everyone
    edges = [(f"P{i}", "P4") for i in range(4)]
    roles = compute_roles(papers, edges)
    assert roles.get("P4") == "foundational"


def test_roles_no_edges_falls_back_to_citations():
    papers = _papers(3)  # P0 has the highest citation_count
    roles = compute_roles(papers, [])
    assert roles.get("P0") == "foundational"
