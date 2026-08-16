"""Cross-encoder reranker tests (tools/retrieval/rerank.py, offline).

The CrossEncoder is stubbed; covers reordering, top_k truncation, the
no-model fallback (original RRF order), disabled-by-config, and the
load-failure singleton (no retry storm).
"""
from __future__ import annotations

import sys
import types

import tools.retrieval.rerank as rr


def setup_function():
    rr.reset_reranker()


def teardown_function():
    rr.reset_reranker()


class _FakeCrossEncoder:
    """Scores passages by whether they contain the query token."""

    def predict(self, pairs):
        return [1.0 if "target" in p else 0.0 for _q, p in pairs]


def test_rerank_reorders_by_score(monkeypatch):
    monkeypatch.setattr(rr, "get_reranker", lambda: _FakeCrossEncoder())
    order = rr.rerank("q", ["filler a", "target passage", "filler c"])
    assert order[0] == 1
    assert sorted(order) == [0, 1, 2]


def test_rerank_top_k_truncates(monkeypatch):
    monkeypatch.setattr(rr, "get_reranker", lambda: _FakeCrossEncoder())
    order = rr.rerank("q", ["filler a", "target b", "filler c"], top_k=2)
    assert order == [1, 0]


def test_rerank_without_model_keeps_original_order(monkeypatch):
    monkeypatch.setattr(rr, "get_reranker", lambda: None)
    assert rr.rerank("q", ["a", "b", "c"], top_k=2) == [0, 1]


def test_rerank_scoring_failure_keeps_order(monkeypatch):
    class _Boom:
        def predict(self, pairs):
            raise RuntimeError("scoring exploded")

    monkeypatch.setattr(rr, "get_reranker", lambda: _Boom())
    assert rr.rerank("q", ["a", "b"]) == [0, 1]


def test_disabled_by_empty_setting(monkeypatch):
    fake_settings = types.SimpleNamespace(
        storage=types.SimpleNamespace(reranker_model="  "))
    monkeypatch.setattr(rr, "get_settings", lambda: fake_settings)
    assert rr.get_reranker() is None


def test_load_failure_not_retried(monkeypatch):
    calls = {"n": 0}

    def _boom(_name):
        calls["n"] += 1
        raise RuntimeError("download failed")

    fake_st = types.SimpleNamespace(CrossEncoder=_boom)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    fake_settings = types.SimpleNamespace(
        storage=types.SimpleNamespace(reranker_model="some/model"))
    monkeypatch.setattr(rr, "get_settings", lambda: fake_settings)

    assert rr.get_reranker() is None
    assert rr.get_reranker() is None
    assert calls["n"] == 1  # singleton: tried once, never again
