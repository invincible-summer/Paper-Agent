"""Tests for the prompt registry + BM25/RRF retrieval primitives."""
from __future__ import annotations

import pytest

from core.prompts.registry import active_versions, get, register
from tools.retrieval.bm25 import BM25Doc, BM25Index, rrf_merge, tokenize


# --- registry ---------------------------------------------------------------

def test_register_and_get():
    register("test.demo", 1, "hello v1")
    assert get("test.demo").text == "hello v1"
    register("test.demo", 2, "hello v2")
    assert get("test.demo").version == 2
    assert get("test.demo").text == "hello v2"


def test_unknown_prompt_lists_registered():
    with pytest.raises(KeyError):
        get("no.such.prompt")


def test_builtin_prompts_registered():
    import core.prompts  # noqa: F401 — triggers registration
    versions = active_versions()
    for pid in ("system.main", "system.redline_tail", "search.understand",
                "map.cluster_labels", "map.landscape", "path.reading_reasons"):
        assert pid in versions, pid


def test_register_rejects_bad_args():
    with pytest.raises(ValueError):
        register("", 1, "x")
    with pytest.raises(ValueError):
        register("a.b", 0, "x")


# --- BM25 -------------------------------------------------------------------

def test_tokenize_mixed_language():
    tokens = tokenize("Graph Neural Networks 图神经网络")
    assert "graph" in tokens and "networks" in tokens
    assert "图神" in tokens and "经网" in tokens  # CJK bigrams


def test_bm25_ranks_relevant_doc_first():
    docs = [
        BM25Doc(id="a", text="graph neural networks for molecular property prediction"),
        BM25Doc(id="b", text="medieval castle architecture in europe"),
    ]
    index = BM25Index(docs)
    hits = index.search("graph neural networks", top_k=2)
    assert hits and hits[0][0].id == "a"
    assert hits[0][1] > 0


def test_bm25_cjk_query():
    docs = [
        BM25Doc(id="a", text="启蒙理性与工具理性批判"),
        BM25Doc(id="b", text="graph neural networks"),
    ]
    index = BM25Index(docs)
    hits = index.search("启蒙理性", top_k=2)
    assert hits and hits[0][0].id == "a"


def test_bm25_empty_query_and_corpus():
    assert BM25Index([BM25Doc(id="a", text="x")]).search("") == []
    assert BM25Index([]).search("anything") == []


def test_rrf_merge_prefers_consensus():
    a = ["x", "y", "z"]
    b = ["y", "x", "w"]
    fused = rrf_merge([a, b])
    # x and y are top-2 in both lists and must outrank z/w
    assert set(fused[:2]) == {"x", "y"}
    assert rrf_merge([a], top_n=2) == ["x", "y"]
