#!/usr/bin/env python3
"""Skill dispatch eval — does the model autonomously pick the right skill?

For each golden utterance, one utility LLM call classifies it against the
skill metadata section (the same text the system prompt carries). This is a
proxy for end-to-end dispatch: it isolates "metadata + utterance → skill"
from the rest of the turn.

  python tests/eval/run_dispatch_eval.py            # online (needs API key)
  pytest tests/test_dispatch_eval.py                # offline: yaml + scorer

Scoring (score_dispatch) is pure and unit-tested.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
GOLDEN_PATH = EVAL_DIR / "dispatch_golden.yaml"

_PROJECT_ROOT = EVAL_DIR.parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def parse_dispatch_golden(raw: list[dict]) -> list[dict]:
    """Validate + normalize golden cases."""
    out = []
    for i, c in enumerate(raw):
        assert c.get("id") and "utterance" in c, f"case #{i} missing id/utterance"
        expect = c.get("expect") or None
        out.append({"id": c["id"], "utterance": str(c["utterance"]),
                    "expect": str(expect) if expect else None})
    return out


def score_dispatch(cases: list[dict], predictions: dict[str, str | None]) -> dict:
    """Pure scorer: accuracy + per-skill precision/recall vs expectations."""
    correct = 0
    per_skill: dict[str, dict[str, int]] = {}
    details = []
    for c in cases:
        exp = c["expect"]
        pred = predictions.get(c["id"])
        ok = exp == pred
        correct += ok
        details.append({"id": c["id"], "expect": exp, "predicted": pred, "ok": ok})
        for label in ({exp, pred} - {None}):
            per_skill.setdefault(label, {"tp": 0, "fp": 0, "fn": 0})
        if exp and pred == exp:
            per_skill[exp]["tp"] += 1
        elif exp and pred != exp:
            per_skill[exp]["fn"] += 1
            if pred:
                per_skill[pred]["fp"] += 1
        elif not exp and pred:
            per_skill[pred]["fp"] += 1
    metrics = {}
    for name, m in per_skill.items():
        prec = m["tp"] / (m["tp"] + m["fp"]) if m["tp"] + m["fp"] else 0.0
        rec = m["tp"] / (m["tp"] + m["fn"]) if m["tp"] + m["fn"] else 0.0
        metrics[name] = {"precision": round(prec, 3), "recall": round(rec, 3)}
    return {"accuracy": round(correct / len(cases), 3) if cases else 0.0,
            "per_skill": metrics, "details": details}


async def classify(utterance: str) -> str | None:
    """One utility LLM call: utterance → skill name or None."""
    from langchain_core.messages import HumanMessage

    from core.llm import ainvoke_utility, get_llm
    from core.skills import skills_prompt_section

    prompt = (
        skills_prompt_section()
        + "\n\n用户请求：" + utterance
        + "\n\n判断该请求命中哪个技能的触发场景。只输出技能名；都不命中输出 none。"
    )
    resp = await ainvoke_utility(get_llm("light"), [HumanMessage(content=prompt)])
    text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
    token = text.split()[0].strip().strip("`。.`") if text else ""
    return None if token.lower() in ("none", "") else token


async def main() -> None:
    from core.skills import all_skills, load_skills

    load_skills()
    valid = {s.name for s in all_skills()}
    cases = parse_dispatch_golden(yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8")))
    predictions: dict[str, str | None] = {}
    for c in cases:
        pred = await classify(c["utterance"])
        if pred is not None and pred not in valid:
            print(f"  [{c['id']}] hallucinated skill {pred!r} → counted as wrong")
        predictions[c["id"]] = pred
    report = score_dispatch(cases, predictions)
    print(f"\naccuracy: {report['accuracy']}")
    for name, m in sorted(report["per_skill"].items()):
        print(f"  {name}: precision={m['precision']} recall={m['recall']}")
    for d in report["details"]:
        if not d["ok"]:
            print(f"  MISS [{d['id']}] expect={d['expect']} got={d['predicted']}")


if __name__ == "__main__":
    asyncio.run(main())
