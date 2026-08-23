"""Tests for the abstract-only network and upload-only full-text boundary."""
from core.models import Paper, PaperSummary
from core.reading_policy import (
    evidence_gap, full_read_allowance, fulltext_available, is_detail_query,
    is_full_text, plan_reading_depth, summary_has_full_text,
)
from agents.session import ChatSession


def _passage(section="abstract"):
    return {"paper_id": "p1", "section": section, "title": "T", "text": "x"}


def test_detail_cues_and_gap_detection():
    assert is_detail_query("准确率提升了多少")
    assert is_detail_query("what is the exact accuracy")
    assert not is_detail_query("研究方向是什么")
    assert evidence_gap("方法未报告，数据集也未报告。", [_passage("methods")], "方向")
    assert evidence_gap("有一些内容", [_passage(), _passage("summary")], "具体数值是多少")
    assert not evidence_gap("有一些内容", [_passage("experiments")], "具体数值是多少")


def test_upload_text_detection_and_retired_remote_helpers():
    assert not is_full_text(None)
    assert not is_full_text("x" * 199)
    assert is_full_text("x" * 200)
    assert not summary_has_full_text(None)
    assert summary_has_full_text(PaperSummary(paper_id="upload:a", full_text="x" * 500))
    assert not fulltext_available(Paper(id="network"))


def test_network_read_budget_is_always_zero_and_depth_is_empty():
    session = ChatSession(session_id="budget")
    assert full_read_allowance(session, 5, 2) == 0
    assert plan_reading_depth(session, [Paper(id="p")], "细节") == ([], [])
