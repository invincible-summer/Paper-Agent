"""Adaptive deep-reading policy — explicit full-text default + evidence gaps.

- ``deep_read`` is an explicit full-text operation: every selected network
  paper gets an OA full-text attempt. Papers whose OA PDF cannot be fetched
  (paywalled / no OA copy / parse failure) degrade to abstract level and are
  reported as abstract — never as full-text.
- ``ask_papers`` remains evidence-sufficiency driven: a generated answer with
  an evidence gap triggers ONE targeted full-text escalation inside the same
  tool call, subject to the ask budget.
- The SQLite summary cache + session vector store make re-reads free. A
  full-mode cache row without real full text is invalid and is self-healed on
  the next read (older versions could cache an abstract fallback under the
  ``full`` mode key).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.session import ChatSession
    from core.models import Paper

# --- budget knobs -------------------------------------------------------------
# Automatic ask_papers escalation budget. Explicit deep_read is not capped here:
# the user asked for deep reading, so every selected paper gets a full-text
# attempt and OA-unavailable papers fall back honestly to abstract level.
FULL_READS_PER_ASK = 2        # max papers escalated inside one ask_papers call
SESSION_FULL_READ_CAP = 8     # soft cap of automatic full-read escalations per session

# A "full text" shorter than this is treated as an abstract/parse fragment, so
# it can never be labelled or cached as full-text level.
MIN_FULL_TEXT_CHARS = 200

# Verified OA full-text availability states carried by Paper.fulltext_status.
# ``unknown`` means "not verified yet" and must never be displayed as available.
FULLTEXT_STATUS_AVAILABLE = "available"
FULLTEXT_STATUS_UNAVAILABLE = "unavailable"
FULLTEXT_STATUS_UNKNOWN = "unknown"
_FULLTEXT_STATUSES = {
    FULLTEXT_STATUS_AVAILABLE, FULLTEXT_STATUS_UNAVAILABLE, FULLTEXT_STATUS_UNKNOWN,
}

# --- multimodal budget knobs --------------------------------------------------
# Vision calls are the most expensive operation in the pipeline. These caps keep
# a single paper's element-understanding cost bounded; excess elements are
# dropped (figures preferred over tables/formulas) rather than silently billed.
VISION_CALLS_PER_PAPER = 12   # max VLM calls per paper (figures+tables+formulas+ocr escalation)
ELEMENTS_PER_PAPER_CAP = 20   # max elements extracted/understood per paper
SCAN_VLM_PAGES_PER_PAPER = 10  # max pages VLM-OCR'd when Docling OCR underperforms

# --- demand-side cues -----------------------------------------------------------
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


def is_detail_query(text: str) -> bool:
    """True when the query/focus asks for precise data an abstract can't carry."""
    q = (text or "").lower()
    if not q:
        return False
    return any(c in q for c in _DETAIL_CUES_ZH) or any(c in q for c in _DETAIL_CUES_EN)


# --- result-side gap detection ---------------------------------------------------
_GAP_MARKERS = (
    "未报告", "无法确定", "无法从", "未见", "没有提供", "未提及", "未给出",
    "not reported", "not specified", "unclear", "not provided",
)


def evidence_gap(answer: str, passages: list[dict], query: str) -> bool:
    """Rule-based sufficiency check on a generated answer. Zero cost.

    Gap = (a) the answer itself flags >=2 unsubstantiated points, or
    (b) a detail question was answered from abstract/summary sections only.
    """
    if not answer:
        return False
    marker_hits = sum(answer.count(m) for m in _GAP_MARKERS)
    if marker_hits >= 2:
        return True
    if is_detail_query(query) and passages:
        sections = {p.get("section", "") for p in passages}
        if sections and sections <= {"abstract", "summary"}:
            return True
    return False


# --- budgeting --------------------------------------------------------------------

def is_full_text(value: str | None) -> bool:
    """True when a stored ``full_text`` is a real parsed body, not an abstract.

    A few hundred chars of recovered text may still be a useful fragment, but it
    is not enough evidence to claim a full-text read; treat it as abstract-level
    so reporting and cache keys stay honest.
    """
    return bool(value) and len((value or "").strip()) >= MIN_FULL_TEXT_CHARS


def summary_has_full_text(summary: object | None) -> bool:
    """True when a PaperSummary/dict actually carries real full text."""
    if summary is None:
        return False
    value = getattr(summary, "full_text", None)
    if value is None and isinstance(summary, dict):
        value = summary.get("full_text")
    return is_full_text(value)


def normalize_fulltext_status(status: str | None) -> str:
    """Coerce an arbitrary status string to one of the three known states."""
    return status if status in _FULLTEXT_STATUSES else FULLTEXT_STATUS_UNKNOWN


def fulltext_available(paper: "Paper | None", summaries: dict | None = None) -> bool:
    """True only for papers whose full text is *actually* available/verified.

    A URL-looking ``pdf_url`` is NOT enough — it may be a paywalled landing
    page. Availability comes from a verified live-PDF probe (search/research
    map), a real deep_read full summary, or a downloaded local PDF.
    """
    if paper is None:
        return False
    status = normalize_fulltext_status(getattr(paper, "fulltext_status", None))
    if status == FULLTEXT_STATUS_AVAILABLE:
        return True
    if summaries and summary_has_full_text(summaries.get(paper.id)):
        return True
    return False


def full_read_allowance(session: "ChatSession", requested: int, per_call: int) -> int:
    """How many automatic full-text escalations this ask call may still attempt."""
    used = getattr(session, "full_read_count", 0)
    remaining = max(0, SESSION_FULL_READ_CAP - used)
    return max(0, min(requested, per_call, remaining))


def plan_reading_depth(
    session: "ChatSession", papers: "list[Paper]", focus: str
) -> "tuple[list[Paper], list[Paper]]":
    """Return full-text candidates for an explicit deep_read call.

    ``deep_read`` defaults to full-text for every selected paper: all papers go
    into the full-attempt group (highest relevance first). The reader then
    downloads OA PDFs and degrades each unavailable paper to abstract level,
    reporting actual levels separately. ``focus`` never silently shrinks the
    full-text group.
    """
    if not papers:
        return [], []
    ordered = sorted(papers, key=lambda p: -(p.relevance_score or 0))
    return ordered, []
