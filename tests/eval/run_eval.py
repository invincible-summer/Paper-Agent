#!/usr/bin/env python3
"""Phase 4 Eval runner (update_plan.md §7.2).

Quality guardrail: run the search pipeline against a golden set and report
recall on expected keywords, classic-paper hits, and core-layer size. Two modes:

  offline: each case's fixture (recorded search result dict) is scored directly
           — no API calls, deterministic. Used in CI / test_eval.py.
  online:  runs the real search_agent (needs API key + network); manual trigger.

Usage:
  python tests/eval/run_eval.py                 # offline (default)
  python tests/eval/run_eval.py --mode online   # real search
  python tests/eval/run_eval.py --id transformers_attention

Scoring functions (parse_golden, score_run, summarize) are pure and unit-tested.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
GOLDEN_PATH = EVAL_DIR / "golden.yaml"
FIXTURES_DIR = EVAL_DIR / "fixtures"

_PROJECT_ROOT = EVAL_DIR.parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@dataclass
class GoldenCase:
    id: str
    topic: str
    language: str = "en"
    expected_keywords: list[str] = field(default_factory=list)
    expected_titles: list[str] = field(default_factory=list)
    min_core: int = 0
    max_core: int = 999
    notes: str = ""

    @property
    def fixture_path(self) -> Path:
        return FIXTURES_DIR / f"{self.id}.json"


@dataclass
class CaseResult:
    id: str
    passed: bool
    keyword_recall: float
    classic_hit: bool
    core_count_ok: bool
    n_core: int
    details: str = ""


def parse_golden(raw: Any) -> list[GoldenCase]:
    """Parse golden.yaml content (already-loaded list of dicts) into cases."""
    cases: list[GoldenCase] = []
    for d in raw:
        cases.append(GoldenCase(
            id=d["id"],
            topic=d["topic"],
            language=d.get("language", "en"),
            expected_keywords=d.get("expected_keywords", []),
            expected_titles=d.get("expected_titles", []),
            min_core=d.get("min_core", 0),
            max_core=d.get("max_core", 999),
            notes=d.get("notes", ""),
        ))
    return cases


def _normalize(s: str) -> str:
    return (s or "").strip().lower()


def score_run(case: GoldenCase, run_result: dict) -> CaseResult:
    """Score a single search run against a golden case (pure function).

    run_result shape mirrors search_agent output: {papers: [Paper-dict], ...}.
    """
    core = run_result.get("papers", [])
    n_core = len(core)

    # --- keyword recall over core-layer title + abstract + keywords ---
    corpus = []
    for p in core:
        corpus.append(_normalize(p.get("title", "")))
        corpus.append(_normalize(p.get("abstract", "")))
        for kw in (p.get("keywords") or []):
            corpus.append(_normalize(str(kw)))
    haystack = " ".join(corpus)
    kws = case.expected_keywords
    if kws:
        hit = sum(1 for k in kws if _normalize(k) in haystack)
        keyword_recall = hit / len(kws)
    else:
        keyword_recall = 1.0

    # --- classic-paper hit (fuzzy title substring) ---
    classic_hit = True
    if case.expected_titles:
        titles = [_normalize(p.get("title", "")) for p in core]
        classic_hit = any(
            any(et in t or t in et for t in titles)
            for et in (_normalize(x) for x in case.expected_titles)
        )

    core_count_ok = case.min_core <= n_core <= case.max_core

    checks = [keyword_recall >= 0.5, classic_hit, core_count_ok]
    passed = all(checks)
    details = (f"recall={keyword_recall:.0%} core={n_core} "
               f"(expect {case.min_core}-{case.max_core})")
    if case.expected_titles:
        details += f" classic={'hit' if classic_hit else 'miss'}"
    return CaseResult(
        id=case.id, passed=passed, keyword_recall=keyword_recall,
        classic_hit=classic_hit, core_count_ok=core_count_ok,
        n_core=n_core, details=details,
    )


def summarize(results: list[CaseResult]) -> dict:
    """Aggregate pass rate + mean recall (pure function)."""
    if not results:
        return {"n": 0, "pass_rate": 0.0, "mean_recall": 0.0}
    n_pass = sum(1 for r in results if r.passed)
    mean_recall = sum(r.keyword_recall for r in results) / len(results)
    return {
        "n": len(results),
        "pass_rate": n_pass / len(results),
        "mean_recall": mean_recall,
    }


def _load_golden() -> list[GoldenCase]:
    import yaml
    with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    return parse_golden(raw)


def run_offline(case: GoldenCase) -> CaseResult:
    """Score a recorded fixture (no API). Fixture = search_agent result dict."""
    if not case.fixture_path.exists():
        return CaseResult(
            id=case.id, passed=False, keyword_recall=0.0, classic_hit=False,
            core_count_ok=False, n_core=0,
            details=f"MISSING fixture: {case.fixture_path.name} (record with --record)",
        )
    with open(case.fixture_path, "r", encoding="utf-8") as f:
        result = json.load(f)
    return score_run(case, result)


async def run_online(case: GoldenCase, record: bool = False) -> CaseResult:
    """Run the real search_agent. Requires API key + network (manual)."""
    from agents.search_agent import search_agent
    state = {
        "topic": case.topic,
        "user_conception": "",
        "language": case.language,
        "field_profile": "general",
        "search_mode": "flash",
        "llm_score_count": 20,
        "deep_read_count": 10,
    }
    result = await search_agent(state)
    cr = score_run(case, result)
    if record:
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        with open(case.fixture_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, default=str, indent=2)
    return cr


def main() -> int:
    parser = argparse.ArgumentParser(description="Eval golden-set runner")
    parser.add_argument("--mode", choices=["offline", "online"], default="offline")
    parser.add_argument("--id", help="run a single case by id")
    parser.add_argument("--record", action="store_true",
                        help="(online) record results as fixtures for offline use")
    args = parser.parse_args()

    cases = _load_golden()
    if args.id:
        cases = [c for c in cases if c.id == args.id]
        if not cases:
            print(f"unknown case id: {args.id}")
            return 2

    results: list[CaseResult] = []
    for case in cases:
        if args.mode == "offline":
            r = run_offline(case)
        else:
            r = asyncio.run(run_online(case, record=args.record))
        results.append(r)
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.id:28s} {r.details}")

    s = summarize(results)
    print(f"\n{int(s['pass_rate']*s['n'])}/{s['n']} passed "
          f"(recall {s['mean_recall']:.0%})")
    return 0 if s["pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
