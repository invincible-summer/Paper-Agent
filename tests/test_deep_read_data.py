"""deep_read returns summaries data + the done-event strips big payloads."""

from __future__ import annotations

import asyncio

import agents.reader_agent as ramod
import agents.tools_impl as ti
from agents.orchestrator import _build_tool_result_message, _lite_tool_calls
from core.models import Paper, PaperSummary
from core.tool_protocol import ok


def test_lite_tool_calls_strips_summaries_and_answer():
    full = [
        {"name": "deep_read",
         "result": {"summary": "Extracted 2.", "summaries": {"P1": {"methodology": "m"}}, "failures": []}},
        {"name": "ask_papers",
         "result": {"summary": "Answered.", "answer": "long answer " * 500, "sources": []}},
    ]
    lite = _lite_tool_calls(full)
    assert lite[0]["result"]["summaries"] is None
    assert lite[0]["result"]["failures"] == []
    assert lite[1]["result"]["answer"] is None
    assert lite[1]["result"]["sources"] == []


def test_tool_deep_read_returns_summaries_data(monkeypatch):
    """_tool_deep_read embeds per-paper summaries + failures in result.data."""

    async def fake_reader(state, progress_callback=None, read_mode="abstract"):
        return {
            "paper_summaries": {"P1": PaperSummary(paper_id="P1", methodology="m")},
            "read_failures": [],
        }

    monkeypatch.setattr(ramod, "reader_agent", fake_reader)

    session = ti.ChatSession(papers=[Paper(id="P1", title="T")])
    result = asyncio.run(ti._tool_deep_read({"mode": "abstract"}, session, None))

    assert not result.is_error
    assert "summaries" in result.data
    assert "failures" in result.data
    assert "P1" in result.data["summaries"]
    assert result.data["summaries"]["P1"].get("methodology") == "m"
    # An abstract-only result must never be reported as full-text level.
    assert "全文级" not in result.text


def test_tool_deep_read_requires_papers():
    session = ti.ChatSession()
    result = asyncio.run(ti._tool_deep_read({"mode": "abstract"}, session, None))
    assert result.is_error
    assert result.error_code == "NO_PAPERS"


def test_tool_deep_read_rejects_unknown_paper_ids():
    session = ti.ChatSession(papers=[Paper(id="P1", title="T")])
    result = asyncio.run(ti._tool_deep_read(
        {"mode": "abstract", "paper_ids": ["NOPE"]}, session, None))
    assert result.is_error
    assert result.error_code == "VALIDATION_ERROR"


def test_tool_deep_read_accepts_normalized_doi_alias(monkeypatch):
    canonical = "doi:10.48550/arXiv.1706.03762"

    async def fake_reader(state, progress_callback=None, read_mode="abstract"):
        assert [p.id for p in state["papers"]] == [canonical]
        return {
            "paper_summaries": {canonical: PaperSummary(paper_id=canonical)},
            "read_failures": [],
        }

    monkeypatch.setattr(ramod, "reader_agent", fake_reader)
    session = ti.ChatSession(papers=[Paper(
        id=canonical, doi="10.48550/arXiv.1706.03762",
        title="Attention Is All You Need",
    )])
    result = asyncio.run(ti._tool_deep_read({
        "paper_ids": ["10.48550/arxiv.1706.03762"],
        "focus": "core method",
    }, session, None))

    assert not result.is_error
    assert canonical in result.data["summaries"]


def test_deep_read_defaults_to_full_attempt_and_reports_actual_levels(monkeypatch):
    """deep_read tries full text for every selected paper and counts only
    summaries that actually carry full_text as full-level."""

    async def fake_reader(state, progress_callback=None, read_mode="full"):
        assert read_mode == "full"
        assert [p.id for p in state["papers"]] == ["P1", "P2"]
        return {
            "paper_summaries": {
                "P1": PaperSummary(paper_id="P1", methodology="m",
                                   full_text="full body " * 60),
                "P2": PaperSummary(paper_id="P2", methodology="m2"),
            },
            "read_failures": [],
            "fulltext_fallbacks": [{
                "paper_id": "P2", "title": "Paywalled paper",
                "reason": "oa_fulltext_unavailable",
            }],
        }

    monkeypatch.setattr(ramod, "reader_agent", fake_reader)
    session = ti.ChatSession(papers=[
        Paper(id="P1", title="Open access paper", relevance_score=0.8),
        Paper(id="P2", title="Paywalled paper", relevance_score=0.5),
    ])
    result = asyncio.run(ti._tool_deep_read({"focus": ""}, session, None))

    assert not result.is_error
    assert "1 篇已取得 OA 全文" in result.text
    assert "1 篇未取得全文" in result.text
    assert "全文级" not in result.text.replace("全文级深读", "")  # only the honest full count uses it
    assert result.data["full_text_paper_ids"] == ["P1"]
    assert result.data["abstract_fallback_papers"][0]["paper_id"] == "P2"
    assert session.full_read_count == 1
    # Verified statuses stay in sync so a later research_map cannot regress to
    # a URL-based guess.
    assert session.papers[0].fulltext_status == "available"
    assert session.papers[1].fulltext_status == "unavailable"


def test_deep_read_never_claims_full_when_no_pdf_was_read(monkeypatch):
    """All attempted papers fall back to abstract → result text says so."""

    async def fake_reader(state, progress_callback=None, read_mode="full"):
        return {
            "paper_summaries": {
                p.id: PaperSummary(paper_id=p.id, methodology="abstract only")
                for p in state["papers"]
            },
            "read_failures": [],
        }

    monkeypatch.setattr(ramod, "reader_agent", fake_reader)
    session = ti.ChatSession(papers=[Paper(id="P1", title="Paywalled")])
    result = asyncio.run(ti._tool_deep_read({}, session, None))

    assert not result.is_error
    assert result.data["full_text_paper_ids"] == []
    assert result.data["abstract_fallback_papers"][0]["paper_id"] == "P1"
    assert "均未取得 OA 全文" in result.text
    assert "全文级深读" not in result.text
    assert session.full_read_count == 0


def test_build_tool_result_message_omits_summaries():
    """The big summaries payload must NOT leak into the LLM context message."""
    r = ok("deep_read", "深读完成：成功提取 1 篇",
           summaries={"P1": {"methodology": "m"}}, failures=[])
    msg = _build_tool_result_message("deep_read", r)
    assert "深读完成" in msg
    assert "methodology" not in msg


def test_tool_deep_read_attachment_only_does_not_read_network_papers(monkeypatch):
    """Selecting an upload must not accidentally deep-read every network paper."""
    import tools.ingest.attachments as attachment_ingest

    called = []

    async def fake_understand(attachment, session, *, focus=""):
        called.append(attachment["id"])
        return {"id": attachment["id"], "status": "ready", "element_count": 1}

    async def forbidden_reader(*args, **kwargs):
        raise AssertionError("network reader called for attachment-only deep_read")

    monkeypatch.setattr(attachment_ingest, "ensure_attachment_understood", fake_understand)
    monkeypatch.setattr(ramod, "reader_agent", forbidden_reader)

    session = ti.ChatSession(papers=[Paper(id="P1", title="Network")])
    session.attachments = [{
        "id": "a" * 32, "filename": "upload.png", "ext": "png",
        "multimodal_status": "pending",
    }]
    result = asyncio.run(ti._tool_deep_read(
        {"attachment_ids": ["a" * 32], "focus": "figure"}, session, None
    ))
    assert not result.is_error
    assert called == ["a" * 32]
    assert result.data["summaries"] == {}


def test_deep_read_payload_includes_title_and_document_metadata(monkeypatch):
    async def fake_reader(state, progress_callback=None, read_mode="full"):
        pid = state["papers"][0].id
        return {
            "paper_summaries": {pid: PaperSummary(
                paper_id=pid, full_text="full body " * 60,
                section_outline=[{"title": "1 Introduction", "page_start": 1, "page_end": 2}],
                document_info={"read_level": "full", "page_count": 2,
                               "section_count": 1, "ocr_status": "not_needed"},
            )},
            "read_failures": [], "fulltext_fallbacks": [],
        }

    monkeypatch.setattr(ramod, "reader_agent", fake_reader)
    session = ti.ChatSession(papers=[Paper(id="P1", title="Readable Paper")])
    result = asyncio.run(ti._tool_deep_read({"paper_ids": ["P1"]}, session, None))
    payload = result.data["summaries"]["P1"]
    assert payload["title"] == "Readable Paper"
    assert payload["section_outline"][0]["title"] == "1 Introduction"
    assert payload["document_info"]["ocr_status"] == "not_needed"


def test_tool_deep_read_reports_deferred_attachment(monkeypatch):
    import tools.ingest.attachments as attachment_ingest

    async def fake_understand(attachment, session, *, focus=""):
        return {
            "id": attachment["id"], "status": "deferred", "element_count": 0,
            "reason": "该格式已安全保存，但当前版本尚未提供解析能力。",
        }

    monkeypatch.setattr(attachment_ingest, "ensure_attachment_understood", fake_understand)
    session = ti.ChatSession()
    session.attachments = [{
        "id": "d" * 32, "filename": "sheet.xlsx", "ext": "xlsx",
        "multimodal_status": "deferred",
    }]
    result = asyncio.run(ti._tool_deep_read(
        {"attachment_ids": ["d" * 32]}, session, None,
    ))
    assert not result.is_error
    assert "1 个格式已保存但解析延期" in result.text
    assert result.data["attachments"][0]["status"] == "deferred"
