"""Offline tests for the skill-dispatch eval: golden yaml validity + scorer."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

import run_dispatch_eval as de


def _cases():
    raw = yaml.safe_load((Path(de.GOLDEN_PATH).read_text(encoding="utf-8")))
    return de.parse_dispatch_golden(raw)


def test_golden_parses_and_covers_all_skills():
    cases = _cases()
    assert len(cases) >= 20
    from core.skills import all_skills, load_skills

    load_skills()
    installed = {s.name for s in all_skills()}
    expected_skills = {c["expect"] for c in cases if c["expect"]}
    # every expectation references an installed instruction skill
    assert expected_skills <= installed
    # every instruction skill has at least one positive case
    instruction_skills = {"compare_papers", "research_gap", "paper_critique",
                          "presentation_prep", "draft_review", "related_work"}
    assert instruction_skills <= expected_skills
    # negative cases exist
    assert any(c["expect"] is None for c in cases)


def test_scorer_perfect_predictions():
    cases = [
        {"id": "a", "utterance": "x", "expect": "compare_papers"},
        {"id": "b", "utterance": "y", "expect": None},
    ]
    report = de.score_dispatch(cases, {"a": "compare_papers", "b": None})
    assert report["accuracy"] == 1.0
    assert report["per_skill"]["compare_papers"] == {"precision": 1.0, "recall": 1.0}


def test_scorer_confusion():
    cases = [
        {"id": "a", "utterance": "x", "expect": "compare_papers"},
        {"id": "b", "utterance": "y", "expect": "research_gap"},
        {"id": "c", "utterance": "z", "expect": None},
    ]
    preds = {"a": "research_gap", "b": None, "c": "compare_papers"}
    report = de.score_dispatch(cases, preds)
    assert report["accuracy"] == 0.0
    rg = report["per_skill"]["research_gap"]
    assert rg["precision"] == 0.0 and rg["recall"] == 0.0
    cp = report["per_skill"]["compare_papers"]
    assert cp["recall"] == 0.0  # missed its positive case
