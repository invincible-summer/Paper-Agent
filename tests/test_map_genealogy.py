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


def test_genealogy_fulltext_only_after_verification(monkeypatch):
    """Regression: an http(s) pdf_url is often a paywalled landing page and
    must not light up the graph's full-text badge. Only a verified live-PDF
    probe or a real full_text summary may."""
    async def no_citations(_papers):
        return []

    monkeypatch.setattr(genealogy_mod, "fetch_citation_edges", no_citations)
    papers = [
        Paper(id="A", title="paywalled landing page",
              pdf_url="https://dl.acm.org/doi/pdf/10.1145/1",
              fulltext_status="unavailable"),
        Paper(id="B", title="verified OA",
              pdf_url="https://arxiv.org/pdf/1.pdf",
              fulltext_status="available"),
        Paper(id="C", title="not checked yet",
              pdf_url="https://example.org/paper.pdf"),
    ]
    graph = asyncio.run(build_genealogy(papers, {}, embed_fn=None))
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert by_id["A"]["fulltext"] is False
    assert by_id["A"]["fulltext_status"] == "unavailable"
    assert by_id["B"]["fulltext"] is True
    assert by_id["B"]["fulltext_status"] == "available"
    assert by_id["C"]["fulltext"] is False
    assert by_id["C"]["fulltext_status"] == "unknown"


# --- clustering / timeline ---------------------------------------------------

def test_cluster_papers_tiny_set():
    papers = _papers(2)
    assert cluster_papers(papers) == {p.id: 0 for p in papers}


def test_cluster_papers_fallback_groups_by_direction():
    papers = [
        Paper(id="A", title="graph neural networks"),
        Paper(id="B", title="medieval castles"),
        Paper(id="C", title="medieval architecture"),
    ]
    dirs = [{"name": "graph neural"}, {"name": "medieval history"}]
    out = cluster_papers(papers, dirs, embed_fn=None)
    assert out["A"] != out["B"]


def test_timeline_groups_by_year():
    papers = _papers(4)  # years 2018..2021
    timeline = build_timeline(papers)
    assert [t["year"] for t in timeline] == [2018, 2019, 2020, 2021]
    assert timeline[0]["papers"][0]["id"] == "P0"


# --- reading path -------------------------------------------------------------

def test_pick_reading_order_roles_and_dedup():
    papers = _papers(6)
    cluster_of = {p.id: i % 2 for i, p in enumerate(papers)}
    roles = {"P0": "foundational"}
    picks = pick_reading_order(papers, cluster_of, roles, max_picks=6)
    ids = [p["paper_id"] for p in picks]
    assert len(ids) == len(set(ids))
    assert ids[0] == "P0" and picks[0]["role"] == "奠基"
    assert len(picks) <= 6
    roles_present = {p["role"] for p in picks}
    assert "桥梁" in roles_present or "前沿" in roles_present


def test_pick_reading_order_empty():
    assert pick_reading_order([], {}, {}) == []


def test_build_genealogy_times_out_optional_citation_enrichment(monkeypatch):
    async def slow_citations(papers):
        await asyncio.sleep(1)
        return [], "available"

    monkeypatch.setattr(genealogy_mod, "fetch_citation_edges", slow_citations)
    monkeypatch.setattr(genealogy_mod, "_CITATION_ENRICHMENT_TIMEOUT_SECONDS", 0.02)
    papers = [
        Paper(id="p1", title="A", abstract="alpha", citation_count=2),
        Paper(id="p2", title="B", abstract="beta", citation_count=1),
    ]
    graph = asyncio.run(build_genealogy(
        papers, {"p1": 0, "p2": 1},
        embed_fn=lambda docs: [[1.0, 0.0], [0.0, 1.0]],
    ))
    assert graph["citation_enrichment_status"] == "timeout"
    assert len(graph["nodes"]) == 2
