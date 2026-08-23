"""Upload-only deep_read behavior and orchestration payload trimming."""

from __future__ import annotations

import asyncio

import agents.tools_impl as ti
from agents.orchestrator import _build_tool_result_message, _lite_tool_calls
from core.models import Paper
from core.tool_protocol import ok


def test_lite_tool_calls_strips_heavy_payloads():
    full = [
        {"name": "deep_read", "result": {
            "summary": "Extracted 2.", "summaries": {"P1": {"methodology": "m"}},
            "failures": [],
        }},
        {"name": "ask_papers", "result": {
            "summary": "Answered.", "answer": "long answer " * 500, "sources": [],
        }},
    ]
    lite = _lite_tool_calls(full)
    assert lite[0]["result"]["summaries"] is None
    assert lite[0]["result"]["failures"] == []
    assert lite[1]["result"]["answer"] is None
    assert lite[1]["result"]["sources"] == []


def test_deep_read_requires_uploaded_attachment():
    session = ti.ChatSession(papers=[Paper(id="P1", title="Network paper")])
    result = asyncio.run(ti._tool_deep_read({}, session, None))
    assert result.is_error
    assert result.error_code == "NO_PAPERS"
    assert "上传" in result.text and "摘要" in result.text


def test_deep_read_rejects_legacy_network_paper_ids():
    session = ti.ChatSession(papers=[Paper(id="P1", title="Network paper")])
    result = asyncio.run(ti._tool_deep_read({"paper_ids": ["P1"]}, session, None))
    assert result.is_error
    assert result.error_code == "VALIDATION_ERROR"
    assert "上传论文文件" in result.text


def test_deep_read_rejects_unknown_attachment_ids():
    session = ti.ChatSession(attachments=[{
        "id": "a" * 32, "filename": "paper.pdf", "ext": "pdf",
    }])
    result = asyncio.run(ti._tool_deep_read(
        {"attachment_ids": ["b" * 32]}, session, None,
    ))
    assert result.is_error
    assert result.error_code == "VALIDATION_ERROR"


def test_deep_read_parses_only_selected_upload(monkeypatch):
    import tools.ingest.attachments as attachment_ingest

    called = []

    async def fake_understand(attachment, session, *, focus=""):
        called.append((attachment["id"], focus))
        return {
            "id": attachment["id"], "status": "ready", "element_count": 1,
            "document_info": {"read_level": "full", "section_count": 3},
        }

    monkeypatch.setattr(attachment_ingest, "ensure_attachment_understood", fake_understand)
    selected_id = "a" * 32
    session = ti.ChatSession(
        papers=[Paper(id="P1", title="Network")],
        attachments=[
            {"id": selected_id, "filename": "上传论文.pdf", "ext": "pdf"},
            {"id": "b" * 32, "filename": "other.txt", "ext": "txt"},
        ],
    )
    result = asyncio.run(ti._tool_deep_read({
        "attachment_ids": [selected_id], "focus": "方法与局限",
    }, session, None))

    assert not result.is_error
    assert called == [(selected_id, "方法与局限")]
    assert result.data["paper_summaries"] == {}
    assert result.data["attachments"][0]["filename"] == "上传论文.pdf"
    assert result.data["attachments"][0]["document_info"]["read_level"] == "full"
    assert "1/1" in result.text and "摘要证据" in result.text


def test_deep_read_reports_deferred_attachment(monkeypatch):
    import tools.ingest.attachments as attachment_ingest

    async def fake_understand(attachment, session, *, focus=""):
        return {
            "id": attachment["id"], "status": "deferred", "element_count": 0,
            "reason": "该格式已安全保存，但当前版本尚未提供解析能力。",
        }

    monkeypatch.setattr(attachment_ingest, "ensure_attachment_understood", fake_understand)
    attachment_id = "d" * 32
    session = ti.ChatSession(attachments=[{
        "id": attachment_id, "filename": "sheet.xlsx", "ext": "xlsx",
        "multimodal_status": "deferred",
    }])
    result = asyncio.run(ti._tool_deep_read(
        {"attachment_ids": [attachment_id]}, session, None,
    ))
    assert not result.is_error
    assert "1 个格式已保存但解析延期" in result.text
    assert result.data["attachments"][0]["status"] == "deferred"


def test_build_tool_result_message_omits_summaries():
    result = ok(
        "deep_read", "深读完成：成功提取 1 篇",
        summaries={"P1": {"methodology": "m"}}, failures=[],
    )
    message = _build_tool_result_message("deep_read", result)
    assert "深读完成" in message
    assert "methodology" not in message
