"""Tests for the rewritten search agent: understanding parse, semantic rerank,
and adaptive tiering (offline; embedder injected).
"""
from __future__ import annotations

import numpy as np
import pytest

from agents.search_agent import (
    adaptive_tier, parse_understanding, rebalance_core_for_fulltext, rerank_papers,
)
from core.models import Paper


# --- parse_understanding ---------------------------------------------------

def test_parse_understanding_valid_json():
    raw = '{"research_goal": "graph neural networks", "sub_directions": [' \
          '{"name": "GNN theory", "queries_en": ["graph neural networks"], "queries_zh": ["图神经网络"]}]}'
    out = parse_understanding(raw, "GNN")
    assert out["research_goal"] == "graph neural networks"
    assert "graph neural networks" in out["queries"]
    assert "图神经网络" in out["queries"]


def test_parse_understanding_strips_code_fence():
    raw = '```json\n{"research_goal": "rl", "sub_directions": [{"name": "a", "queries_en": ["q1"]}]}\n```'
    out = parse_understanding(raw, "rl")
    assert "q1" in out["queries"]


def test_parse_understanding_garbage_falls_back_to_topic():
    out = parse_understanding("not json at all", "my topic")
    assert out["queries"] == ["my topic"]
    assert out["research_goal"] == "my topic"


# --- rerank_papers ---------------------------------------------------------

def _fake_embed(texts):
    """Deterministic char-bucket embeddings; similar text -> high cosine.

    ord(ch), not hash(ch): str hashing is salted per process, which made
    this "deterministic" embedder randomly flip the ordering between runs.
    """
    out = []
    for t in texts:
        v = np.zeros(16)
        for ch in (t or "").lower():
            v[ord(ch) % 16] += 1.0
        n = np.linalg.norm(v)
        out.append((v / n).tolist() if n > 0 else v.tolist())
    return out


def test_rerank_semantic_orders_relevant_first():
    relevant = Paper(id="P1", title="graph neural networks for molecules",
                     abstract="we study graph neural networks", citation_count=5, year=2023)
    off_topic = Paper(id="P2", title="completely unrelated zoo biology",
                      abstract="totally different words xyz", citation_count=5000, year=2020)
    scored = rerank_papers([off_topic, relevant], "graph neural networks", embed_fn=_fake_embed)
    assert scored[0].id == "P1"
    assert 0 <= scored[0].relevance_score <= 1


def test_rerank_without_embedder_degrades_to_overlap():
    relevant = Paper(id="P1", title="graph neural networks", citation_count=5, year=2023)
    off_topic = Paper(id="P2", title="zoo biology", citation_count=5000, year=2020)
    scored = rerank_papers([off_topic, relevant], "graph neural networks", embed_fn=None)
    assert scored[0].id == "P1"


def test_rerank_empty():
    assert rerank_papers([], "q", embed_fn=_fake_embed) == []


# --- adaptive_tier ---------------------------------------------------------

def _scored(scores):
    return [Paper(id=f"P{i}", title=f"t{i}", relevance_score=s) for i, s in enumerate(scores)]


def test_tier_respects_threshold_and_caps():
    papers = _scored([0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5, 0.45,
                      0.4, 0.38, 0.36, 0.2, 0.1])
    core, candidates = adaptive_tier(papers)
    assert 5 <= len(core) <= 12
    assert len(core) + len(candidates) <= 25
    assert all(p.relevance_score >= 0.35 for p in core + candidates)
    assert core[0].layer == "core"
    if candidates:
        assert candidates[0].layer == "search"


def test_tier_elbow_cut():
    # sharp drop (still above the candidate floor) after rank 6 -> core cut at 6
    papers = _scored([0.9, 0.88, 0.86, 0.84, 0.82, 0.80, 0.55, 0.53, 0.51])
    core, candidates = adaptive_tier(papers)
    assert len(core) == 6
    assert len(candidates) >= 1


def test_tier_min_core_guarantee():
    # only 2 papers above the relative floor -> min_core fills from the top
    papers = _scored([0.9, 0.3, 0.29, 0.28, 0.27, 0.26, 0.1])
    core, _ = adaptive_tier(papers)
    assert len(core) >= 5


def test_tier_empty():
    assert adaptive_tier([]) == ([], [])


# --- verified full-text core rebalance -------------------------------------

def _paper(pid, score, status, layer="search"):
    return Paper(id=pid, title=pid, relevance_score=score,
                 fulltext_status=status, layer=layer)


def test_rebalance_promotes_highest_available_without_backfill():
    core = [_paper("C1", .9, "unavailable", "core"),
            _paper("C2", .8, "available", "core")]
    candidates = [
        _paper("A2", .6, "available"), _paper("U", .95, "unknown"),
        _paper("A1", .7, "available"), _paper("N", .5, "unavailable"),
        _paper("A3", .4, "available"), _paper("A4", .3, "available"),
    ]
    new_core, new_candidates, promoted = rebalance_core_for_fulltext(core, candidates)
    assert promoted == ["A1", "A2", "A3", "A4"]
    assert [p.id for p in new_core] == ["C1", "C2", "A1", "A2", "A3", "A4"]
    assert [p.id for p in new_candidates] == ["U", "N"]
    assert all(p.layer == "core" for p in new_core)
    assert all(p.layer == "search" for p in new_candidates)


def test_rebalance_target_is_total_available_when_fewer_than_five():
    core = [_paper("C", .9, "unavailable", "core")]
    candidates = [_paper("A", .8, "available"), _paper("U", .7, "unknown")]
    new_core, new_candidates, promoted = rebalance_core_for_fulltext(core, candidates)
    assert promoted == ["A"]
    assert [p.id for p in new_core] == ["C", "A"]
    assert [p.id for p in new_candidates] == ["U"]


def test_rebalance_does_nothing_when_core_already_has_target():
    core = [_paper(f"C{i}", 1 - i / 10, "available", "core") for i in range(5)]
    candidates = [_paper("A", .4, "available")]
    new_core, new_candidates, promoted = rebalance_core_for_fulltext(core, candidates)
    assert promoted == []
    assert new_core == core
    assert new_candidates == candidates


def test_rebalance_deduplicates_catalogue_with_core_priority():
    shared_core = _paper("DUP", .9, "unavailable", "core")
    duplicate_candidate = _paper("DUP", .8, "available")
    readable = _paper("A", .7, "available")
    core, candidates, promoted = rebalance_core_for_fulltext(
        [shared_core, shared_core], [duplicate_candidate, readable, readable]
    )
    assert [p.id for p in core] == ["DUP", "A"]
    assert candidates == []
    assert promoted == ["A"]
