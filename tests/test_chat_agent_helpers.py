"""Tests for the new agent helpers: pydantic arg validation, rule reflector,
and the tool-result context message (agents/tools_impl.py + orchestrator.py).
"""

import asyncio

from agents import search_agent as search_agent_mod
from agents import tools_impl as ti
from agents.orchestrator import _build_tool_result_message
from agents.session import ChatSession
from agents.tools_impl import reflect_tool_result, validate_args
from core.models import Paper
from core.tool_protocol import ErrorCode, err, ok


def test_validate_args_missing_required():
    args, err_result = validate_args("search_papers", {})
    assert args is None
    assert err_result.is_error
    assert err_result.error_code == ErrorCode.VALIDATION_ERROR
    assert "topic" in err_result.error["message"]


def test_validate_applies_defaults():
    validated, err_result = validate_args("search_papers", {"topic": "x"})
    assert err_result is None
    assert validated["topic"] == "x"
    assert validated["language"] == "both"
    assert validated["conception"] == ""


def test_validate_choices_rejected():
    _, err_result = validate_args("search_papers", {"topic": "x", "language": "klingon"})
    assert err_result.is_error
    assert err_result.error_code == ErrorCode.VALIDATION_ERROR


def test_validate_deep_read_focus_and_paper_ids():
    validated, err_result = validate_args("deep_read", {"focus": "实验数据集与指标数值"})
    assert err_result is None
    assert validated["focus"] == "实验数据集与指标数值"
    assert validated["paper_ids"] == []
    validated, _ = validate_args("deep_read", {})
    assert validated["focus"] == ""


def test_validate_unknown_tool():
    _, err_result = validate_args("mystery_tool", {"any": 1})
    assert err_result.is_error
    assert err_result.error_code == ErrorCode.NO_TOOL


def test_validate_ask_papers_top_k_clamped_by_schema():
    _, err_result = validate_args("ask_papers", {"query": "q", "top_k": 99})
    assert err_result.is_error  # le=10 enforced by pydantic


def test_build_message_success_includes_summary():
    r = ok("search_papers", "检索完成：核心集 3 篇", total_papers=3, core_titles=["A", "B"])
    msg = _build_tool_result_message("search_papers", r)
    assert "完成" in msg
    assert "检索完成：核心集 3 篇" in msg


def test_build_message_error_surfaces_code():
    r = err("search_papers", ErrorCode.NO_PAPERS, "没有检索到论文")
    msg = _build_tool_result_message("search_papers", r)
    assert "NO_PAPERS" in msg
    assert "没有检索到论文" in msg


def test_build_message_search_exposes_candidate_paper_ids():
    """Candidates must be directly addressable; otherwise the model re-searches
    just to locate a paper the session already owns."""
    r = ok(
        "search_papers", "检索完成",
        papers=[{"id": "doi:10.1/c", "title": "Core Paper"}],
        candidates=[{"id": "doi:10.1145/3661821", "title": "Candidate Survey"}],
    )
    msg = _build_tool_result_message("search_papers", r)
    assert "doi:10.1/c | Core Paper" in msg
    assert "doi:10.1145/3661821 | Candidate Survey" in msg
    assert "不要为了定位它们再次 search_papers" in msg


def test_session_context_lists_core_and_candidate_ids():
    session = ChatSession(
        topic="t",
        papers=[Paper(id="doi:10.1/c", title="Core Paper")],
        candidates=[Paper(id="doi:10.1145/3661821", title="Candidate Survey")],
    )
    text = session.context_summary()
    assert "doi:10.1/c | 全文待验证 | Core Paper" in text
    assert "doi:10.1145/3661821 | 全文待验证 | Candidate Survey" in text
    assert "不要为这些论文再次 search_papers" in text
    assert "禁止再次 search_papers" in text


def test_search_papers_result_carries_verified_fulltext_statuses(monkeypatch):
    """The search tool must expose per-paper fulltext_status for the frontend
    and the LLM, so '哪些论文能看全文' is answerable without deep_read-all."""
    async def fake_search(state, progress_callback=None):
        return {
            "papers": [Paper(id="P1", title="OA paper",
                             fulltext_status="available")],
            "candidates": [Paper(id="P2", title="Paywalled",
                                 fulltext_status="unavailable")],
            "sub_directions": [], "search_queries": [],
            "fulltext_statuses": {"P1": "available", "P2": "unavailable"},
        }

    monkeypatch.setattr(search_agent_mod, "search_agent", fake_search)
    session = ChatSession()
    result = asyncio.run(ti._tool_search_papers({"topic": "x"}, session, None))

    assert not result.is_error
    assert result.data["papers"][0]["fulltext_status"] == "available"
    assert result.data["candidates"][0]["fulltext_status"] == "unavailable"
    assert "1 篇已探测到可访问的 OA PDF" in result.text
    assert "1 篇已探测为不可获取" in result.text
    assert session.papers[0].fulltext_status == "available"


def test_search_papers_rejects_relocating_an_owned_paper(monkeypatch):
    """A search whose topic is a session paper title/DOI must fail fast and
    direct the model to deep_read/ask_papers with the existing id."""
    called = {"n": 0}

    async def forbidden_search(state, progress_callback=None):
        called["n"] += 1
        raise AssertionError("search_agent must not run for an owned paper")

    monkeypatch.setattr(search_agent_mod, "search_agent", forbidden_search)
    session = ti.ChatSession(papers=[Paper(
        id="doi:10.1145/3661821",
        title="A Survey of Graph Neural Networks for Social Recommender Systems",
    )])

    result = asyncio.run(ti._tool_search_papers({
        "topic": "A Survey of Graph Neural Networks for Social Recommender Systems",
    }, session, None))
    assert result.is_error
    assert result.error_code == "VALIDATION_ERROR"
    assert "deep_read" in result.error["message"]
    assert called["n"] == 0

    # DOI alias form is caught by paper_by_id before search_agent runs too.
    result2 = asyncio.run(ti._tool_search_papers({
        "topic": "https://doi.org/10.1145/3661821",
    }, session, None))
    assert result2.is_error
    assert called["n"] == 0


def test_build_message_ask_papers_includes_answer():
    r = ok("ask_papers", "完成回答", answer=" grounded answer body ", sources=[])
    msg = _build_tool_result_message("ask_papers", r)
    assert "grounded answer body" in msg


def test_reflector_skips_errors():
    r = err("search_papers", ErrorCode.NO_PAPERS, "none")
    assert reflect_tool_result("search_papers", r) is None


def test_reflector_short_review_warns():
    r = ok("write_review", "综述已生成", literature_review="short", review_chars=100)
    w = reflect_tool_result("write_review", r)
    assert w is not None and "综述" in w["warning"]


def test_reflector_ok_result_passes():
    r = ok("write_review", "综述已生成", literature_review="x" * 1000, review_chars=1000)
    assert reflect_tool_result("write_review", r) is None
