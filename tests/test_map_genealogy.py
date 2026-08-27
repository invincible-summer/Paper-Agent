"""Tests for genealogy graph data + research map / reading path helpers."""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from agents.map_agent import (
    _fallback_landscape,
    _summarize_map,
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
    """Word-hash embeddings: texts sharing words are close, others ~orthogonal.

    Uses crc32 (not builtin hash) so results are stable across processes.
    """
    import zlib
    out = []
    for t in texts:
        v = np.zeros(32)
        for w in (t or "").lower().split():
            v[zlib.crc32(w.encode()) % 32] += 1.0
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


# --- research-map summary / normalized landscape ------------------------------

def _summary_papers() -> dict[str, Paper]:
    return {
        "P0": Paper(id="P0", title="paper 0", year=2018, citation_count=50),
        "P1": Paper(id="P1", title="paper 1", year=2019, citation_count=40),
        "P2": Paper(id="P2", title="paper 2", year=2020, citation_count=10),
    }


def test_map_summary_prompt_v3_requires_normalized_landscape():
    from core.prompts.registry import get
    prompt = get("map.summary")
    assert prompt.version == 3
    assert "【簇label】（N 篇，起始年–结束年）" in prompt.text
    assert "代表论文" in prompt.text and "禁止编造" in prompt.text


def test_fallback_landscape_normalized_per_cluster_format():
    clusters = [
        {"id": 0, "label": "图神经网络", "paper_ids": ["P0", "P1"]},
        {"id": 1, "label": "知识图谱", "paper_ids": ["P2"]},
    ]
    text = _fallback_landscape(clusters, _summary_papers(), "zh")
    assert text.startswith("该论文集覆盖 2018–2020 年")
    assert "【图神经网络】（2 篇，2018–2019）：《paper 0》（2018）、《paper 1》（2019）" in text
    assert "【知识图谱】（1 篇，2020）：《paper 2》（2020）" in text
    english = _fallback_landscape(clusters, _summary_papers(), "en")
    assert "[图神经网络] (2 papers, 2018–2019): \"paper 0\" (2018)、\"paper 1\" (2019)" in english


def test_summarize_map_parses_labels_and_caps_landscape(monkeypatch):
    from agents import map_agent

    class _FakeResp:
        content = json.dumps({
            "clusters": [{"id": "0", "label": "新簇名", "overview": "概述"}],
            "landscape": "长" * 3000,
        })

    async def fake_ainvoke(llm, messages):
        assert "（2018，被引 50）" in messages[0].content  # input carries year + citations
        return _FakeResp()

    monkeypatch.setattr(map_agent, "ainvoke_utility", fake_ainvoke)
    monkeypatch.setattr(map_agent, "get_llm", lambda role: object())
    clusters = [{"id": 0, "label": "主题簇 1", "overview": "", "paper_ids": ["P0", "P1"]}]
    landscape, status = asyncio.run(
        map_agent._summarize_map(clusters, _summary_papers(), "zh"))
    assert status == "available"
    assert len(landscape) == 2000
    assert clusters[0]["label"] == "新簇名"
    assert clusters[0]["overview"] == "概述"
