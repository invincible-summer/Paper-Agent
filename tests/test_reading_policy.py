"""Tests for the adaptive deep-reading policy + ask_papers escalation wiring."""
from __future__ import annotations

import pytest

import agents.tools_impl as ti
from agents.session import ChatSession
from core.models import Paper
from core.reading_policy import (
    FULL_READS_PER_ASK,
    SESSION_FULL_READ_CAP,
    evidence_gap,
    full_read_allowance,
    is_detail_query,
    is_full_text,
    plan_reading_depth,
    summary_has_full_text,
)


# --- cue matching -----------------------------------------------------------

def test_detail_query_cues():
    assert is_detail_query("这篇论文的准确率提升了多少？")
    assert is_detail_query("what is the exact accuracy on CIFAR-10?")
    assert is_detail_query("超参数设置和消融实验")
    assert not is_detail_query("这篇文章研究的是什么方向")
    assert not is_detail_query("")


# --- gap detection ----------------------------------------------------------

def _passage(section="abstract", pid="p1"):
    return {"paper_id": pid, "section": section, "title": "T", "text": "x"}


def test_evidence_gap_markers():
    passages = [_passage("methods")]  # full-text section present
    assert evidence_gap("方法未报告，数据集也未报告。", passages, "方向问题")
    assert not evidence_gap("方法是 GCN [p1]。数据完整。", passages, "方向问题")


def test_evidence_gap_abstract_only_for_detail_question():
    passages = [_passage("abstract"), _passage("summary", "p2")]
    assert evidence_gap("有一些内容", passages, "具体数值是多少")
    # same passages, non-detail question → no gap
    assert not evidence_gap("有一些内容", passages, "研究的是什么方向")
    # full-text section present → no gap even for detail question
    passages2 = [_passage("experiments")]
    assert not evidence_gap("有一些内容", passages2, "具体数值是多少")


# --- budgeting ----------------------------------------------------------------

def test_full_read_allowance_caps():
    s = ChatSession(session_id="t-budget")
    assert full_read_allowance(s, 5, FULL_READS_PER_ASK) == FULL_READS_PER_ASK
    s.full_read_count = SESSION_FULL_READ_CAP - 1
    assert full_read_allowance(s, 5, FULL_READS_PER_ASK) == 1
    s.full_read_count = SESSION_FULL_READ_CAP
    assert full_read_allowance(s, 5, FULL_READS_PER_ASK) == 0


def test_plan_reading_depth_defaults_to_full_attempts():
    """deep_read is explicit: every selected paper gets a full-text attempt."""
    papers = [Paper(id=f"p{i}", relevance_score=float(i)) for i in range(5)]
    s = ChatSession(session_id="t-plan")

    # Detail focus no longer caps the full group at 3.
    full, abstract = plan_reading_depth(s, papers, "实验数据集与指标数值")
    assert len(full) == 5 and abstract == []
    assert [p.id for p in full] == ["p4", "p3", "p2", "p1", "p0"]  # relevance order

    # Neutral/default focus behaves the same: all selected attempt full text.
    full2, abstract2 = plan_reading_depth(s, papers, "")
    assert len(full2) == 5 and abstract2 == []

    # Empty selection stays empty.
    assert plan_reading_depth(s, [], "指标") == ([], [])


def test_full_text_detection():
    from core.models import PaperSummary
    assert not is_full_text(None)
    assert not is_full_text("")
    assert not is_full_text("x" * 199)
    assert is_full_text("x" * 200)
    assert not summary_has_full_text(None)
    assert not summary_has_full_text({"full_text": "short"})
    assert summary_has_full_text(PaperSummary(paper_id="p", full_text="x" * 500))


# --- ask_papers escalation wiring ----------------------------------------------

@pytest.mark.anyio
async def test_ask_papers_escalates_on_evidence_gap(monkeypatch):
    """Gap answer → full-text escalation → re-retrieve → regenerate."""
    session = ChatSession(session_id="t-esc", papers=[Paper(id="p1", title="T")])
    calls = {"gen": 0, "fetch": 0}

    passages_abs = [_passage("abstract")]
    passages_full = [_passage("experiments")]

    async def fake_generate(query, passages):
        calls["gen"] += 1
        if calls["gen"] == 1:
            return "数据集规模未报告，超参设置也未报告。"  # gap answer
        return "数据集 5 万条，准确率 92.3% [p1]。"

    async def fake_fetch(sess, pid, progress_cb=None):
        calls["fetch"] += 1
        return True

    monkeypatch.setattr(ti, "_generate_grounded_answer", fake_generate)
    monkeypatch.setattr(ti, "_ensure_fulltext", fake_fetch)
    monkeypatch.setattr(ti, "_rewrite_query", lambda q: asyncio_return(q))
    monkeypatch.setattr(ti, "_ensure_session_index", lambda s: None)
    monkeypatch.setattr(
        ti, "_retrieve_passages",
        lambda s, q, pid, k, modality=None: passages_abs if calls["fetch"] == 0 else passages_full)

    result = await ti._tool_ask_papers({"query": "数据集规模和准确率是多少"}, session, None)
    assert not result.is_error
    assert calls["fetch"] >= 1          # escalation happened
    assert calls["gen"] == 2            # regenerated after escalation
    assert "92.3%" in result.data["answer"]
    assert "自动补读全文" in result.text
    assert session.full_read_count >= 1


@pytest.mark.anyio
async def test_ask_papers_no_escalation_when_answer_sufficient(monkeypatch):
    session = ChatSession(session_id="t-noesc", papers=[Paper(id="p1", title="T")])

    async def fake_generate(query, passages):
        return "研究方向是 GNN 推荐系统 [p1]。"

    async def fake_fetch(sess, pid, progress_cb=None):
        raise AssertionError("must not escalate")

    monkeypatch.setattr(ti, "_generate_grounded_answer", fake_generate)
    monkeypatch.setattr(ti, "_ensure_fulltext", fake_fetch)
    monkeypatch.setattr(ti, "_rewrite_query", lambda q: asyncio_return(q))
    monkeypatch.setattr(ti, "_ensure_session_index", lambda s: None)
    monkeypatch.setattr(ti, "_retrieve_passages",
                        lambda s, q, pid, k, modality=None: [_passage("abstract")])

    result = await ti._tool_ask_papers({"query": "这篇研究什么方向"}, session, None)
    assert not result.is_error
    assert "GNN" in result.data["answer"]


async def asyncio_return(x):
    return x
