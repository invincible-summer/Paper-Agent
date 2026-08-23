"""Evidence-bound reading helpers.

Network papers stop at the current valid abstract.  Full-document parsing is
available only for user-uploaded files.  This module keeps the zero-cost
question/detail-gap heuristics and multimodal safety caps; it no longer owns a
remote fetch policy or a network full-text status state machine.
"""
from __future__ import annotations

MIN_FULL_TEXT_CHARS = 200
VISION_CALLS_PER_PAPER = 12
ELEMENTS_PER_PAPER_CAP = 20
SCAN_VLM_PAGES_PER_PAPER = 10

_DETAIL_CUES_ZH = (
    "多少", "数值", "指标", "准确率", "精确", "超参", "消融", "步骤",
    "第几步", "公式", "算法细节", "数据集规模", "提升了多少", "具体数字",
    "实验细节", "对比实验", "基线", "参数设置", "复现", "细节",
)
_DETAIL_CUES_EN = (
    "how many", "how much", "exact", "exactly", "what number", "accuracy",
    "metric", "hyperparameter", "ablation", "dataset size", "baseline",
    "improvement of", "reproduce", "in detail",
)
_GAP_MARKERS = (
    "未报告", "无法确定", "无法从", "未见", "没有提供", "未提及", "未给出",
    "摘要证据不足", "not reported", "not specified", "unclear", "not provided",
)


def is_detail_query(text: str) -> bool:
    q = (text or "").casefold()
    return bool(q) and (any(c in q for c in _DETAIL_CUES_ZH) or any(c in q for c in _DETAIL_CUES_EN))


def is_full_text(value: str | None) -> bool:
    """True when an uploaded sidecar/parsed document is substantial."""
    return bool(value) and len((value or "").strip()) >= MIN_FULL_TEXT_CHARS


def evidence_gap(answer: str, passages: list[dict], query: str) -> bool:
    """Detect likely abstract insufficiency without making another LLM call."""
    if not answer:
        return False
    if sum(answer.count(marker) for marker in _GAP_MARKERS) >= 2:
        return True
    if is_detail_query(query) and passages:
        sections = {str(p.get("section") or "") for p in passages}
        if sections and sections <= {"abstract", "summary"}:
            return True
    return False


# Compatibility reads for old extensions.  They are deliberately inert and
# never authorize network access or historical full-text reuse.
def summary_has_full_text(summary: object | None) -> bool:
    value = getattr(summary, "full_text", None)
    if value is None and isinstance(summary, dict):
        value = summary.get("full_text")
    return is_full_text(value)


def fulltext_available(*_args, **_kwargs) -> bool:
    return False


def full_read_allowance(_session, requested: int, per_call: int) -> int:
    return 0


def plan_reading_depth(_session, _papers: list, _focus: str):
    return [], []
