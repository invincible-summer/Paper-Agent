"""Phase 4 Eval: pure-function tests for the golden-set scorer (offline, no API).

Covers parse_golden, score_run (keyword recall, classic hit, core-count bounds),
and summarize. Uses synthetic run results so no network/fixtures are needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

import run_eval as ev


def _case(**kw):
    base = dict(id="t1", topic="x", expected_keywords=["attention", "transformer"],
                min_core=2, max_core=10)
    base.update(kw)
    return ev.GoldenCase(**base)


def _paper(title, abstract="", keywords=None):
    return {"title": title, "abstract": abstract, "keywords": keywords or []}


def test_parse_golden_roundtrip():
    raw = [{"id": "a", "topic": "T", "expected_keywords": ["k"], "min_core": 1}]
    cases = ev.parse_golden(raw)
    assert len(cases) == 1
    assert cases[0].id == "a" and cases[0].expected_keywords == ["k"]


def test_parse_golden_defaults():
    cases = ev.parse_golden([{"id": "b", "topic": "T"}])
    assert cases[0].language == "en"
    assert cases[0].max_core == 999 and cases[0].expected_titles == []


def test_score_keyword_recall_full():
    case = _case()
    result = {
        "papers": [_paper("Attention Transformer", "self-attention model"),
                   _paper("Other", "transformer survey")]}
    r = ev.score_run(case, result)
    assert r.keyword_recall == 1.0
    assert r.core_count_ok and r.passed


def test_score_keyword_recall_partial_fails_threshold():
    case = _case(expected_keywords=["attention", "nonexistent_kw"])
    result = {"papers": [_paper("Attention paper", "about attention")]}
    r = ev.score_run(case, result)
    assert r.keyword_recall == 0.5  # 1 of 2
    assert not r.passed  # threshold is >=0.5 inclusive, but core count (1) < min 2


def test_score_keyword_recall_zero():
    case = _case()
    result = {"papers": [_paper("Unrelated", "biology and chemistry")]}
    r = ev.score_run(case, result)
    assert r.keyword_recall == 0.0 and not r.passed


def test_score_keywords_checked_in_abstract_and_keywords():
    case = _case(expected_keywords=["reward"])
    result = {"papers": [_paper("RL", "policy and reward maximization")]}
    r = ev.score_run(case, result)
    assert r.keyword_recall == 1.0


def test_core_count_bounds():
    case = _case(min_core=3, max_core=5, expected_keywords=["a"])
    # 2 core papers -> below min
    r = ev.score_run(case, {"papers": [_paper("a"), _paper("b")]})
    assert not r.core_count_ok
    # 6 -> above max
    r2 = ev.score_run(case, {"papers": [_paper("a")] * 6})
    assert not r2.core_count_ok


def test_classic_hit_fuzzy():
    case = _case(expected_titles=["Attention Is All You Need"], expected_keywords=["a"])
    # min_core=2 default, so provide >=2 papers to pass core-count too.
    result = {"papers": [_paper("Attention is All You Need (2017)", "a"), _paper("Other", "a")]}
    r = ev.score_run(case, result)
    assert r.classic_hit and r.passed


def test_classic_miss():
    case = _case(expected_titles=["Nonexistent Classic Paper"], expected_keywords=["a"])
    result = {"papers": [_paper("Something Else", "a")]}
    r = ev.score_run(case, result)
    assert not r.classic_hit and not r.passed


def test_summarize_aggregates():
    rs = [
        ev.CaseResult(id="a", passed=True, keyword_recall=1.0, classic_hit=True, core_count_ok=True, n_core=5),
        ev.CaseResult(id="b", passed=False, keyword_recall=0.5, classic_hit=False, core_count_ok=True, n_core=2),
    ]
    s = ev.summarize(rs)
    assert s["n"] == 2
    assert s["pass_rate"] == 0.5
    assert abs(s["mean_recall"] - 0.75) < 1e-9


def test_summarize_empty():
    assert ev.summarize([])["pass_rate"] == 0.0


def test_run_offline_missing_fixture_reports_fail():
    case = _case(id="definitely_missing_xyz")
    r = ev.run_offline(case)
    assert not r.passed and "MISSING" in r.details


def test_run_offile_with_synthetic_fixture(tmp_path, monkeypatch):
    """A recorded fixture (synthetic) is scored in offline mode."""
    monkeypatch.setattr(ev, "FIXTURES_DIR", tmp_path)
    case = _case(id="fx1", expected_keywords=["attention"])
    fixture = {"papers": [_paper("Attention paper", "self-attention"), _paper("x", "y")]}
    (tmp_path / "fx1.json").write_text(
        __import__("json").dumps(fixture), encoding="utf-8")
    r = ev.run_offline(case)
    assert r.passed and r.n_core == 2
