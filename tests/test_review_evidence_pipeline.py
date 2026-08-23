"""Evidence-bound literature-review regressions."""
from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

from agents.review_agent import (
    REVIEW_GENERATION_BUDGETS,
    _minimum_target,
    _paper_link,
    _prepare_upload_digests,
    _render_evidence_citations,
    _validate_citations,
    prepare_review_evidence,
    review_agent,
)
from agents.session import ChatSession
from core.models import Paper
from core.prompts.registry import get
from tools.export.report import write_reports
import tools.writing.manuscript_export as manuscript_export


def _paper(pid: str, abstract: str, *, source: str = "openalex") -> Paper:
    return Paper(id=pid, title=f"标题 {pid}", abstract=abstract, source=source,
                 urls={source: f"https://{source}.example/{pid}"})


def test_evidence_includes_core_and_candidates_but_skips_empty_abstract():
    state = {
        "papers": [_paper("core", "核心摘要")],
        "candidates": [_paper("candidate", "候选摘要"), _paper("empty", "")],
        "attachments": [],
    }
    result = prepare_review_evidence(state)
    assert [row["id"] for row in result["included"]] == ["core", "candidate"]
    assert any(row["id"] == "empty" and row["reason"] == "missing_or_disabled_abstract"
               for row in result["excluded"])


def test_disabled_abstract_cannot_be_replaced_by_historical_summary(monkeypatch):
    import agents.review_agent as review
    monkeypatch.setattr(review, "paper_abstract_text", lambda paper: "")
    state = {"papers": [_paper("p", "曾经的摘要")],
             "paper_summaries": {"p": {"full_text": "旧全文"}}, "attachments": []}
    result = prepare_review_evidence(state)
    assert result["included"] == []
    assert result["excluded"][0]["reason"] == "missing_or_disabled_abstract"


def test_uploaded_tail_is_present_in_complete_chunks_and_fallback_review(monkeypatch):
    import agents.review_agent as review

    class FakeLLM:
        async def ainvoke(self, _messages):
            class Response:
                content = ""
            return Response()

    monkeypatch.setattr(review, "get_llm", lambda name="light": FakeLLM())
    tail = "TAIL_SECTION_RESEARCH_GAP_2026"
    state = {
        "topic": "上传论文主题",
        "papers": [_paper("p", "网络摘要")],
        "candidates": [],
        "attachments": [{"id": "a1", "filename": "用户论文.txt"}],
        "attachment_texts": {"a1": "引言 " + "x" * 3000 + "\n结论与研究空白：" + tail},
    }
    evidence = prepare_review_evidence(state)
    upload = next(row for row in evidence["included"] if row["kind"] == "upload")
    assert tail in upload["source_text"]
    assert any(tail in str(chunk) for chunk in upload["chunks"])
    result = asyncio.run(review_agent(state))
    assert tail in result["literature_review"]
    assert "# 引言" in result["literature_review"]
    assert "# 纳入文献与证据层级说明" in result["literature_review"]


def test_citation_rendering_distinguishes_upload_filename_and_network_landing():
    network = _paper("p", "摘要")
    upload = {"id": "upload:a1", "kind": "upload", "filename": "我的论文.pdf"}
    text = _render_evidence_citations(
        "网络 [p]，上传 [upload:a1]。",
        {"p": {"kind": "network", "title": network.title, "landing_url": _paper_link(network)},
         "upload:a1": upload},
    )
    assert "标题 p ([Link](https://openalex.example/p))" in text
    assert "我的论文.pdf" in text
    assert ".pdf)" not in text


def test_review_prompts_are_registered_and_have_explicit_budgets():
    for prompt_id in ("review.upload_chunk", "review.outline", "review.theme", "review.synthesis", "review.revision", "review.expansion"):
        assert get(prompt_id).version >= 1
        assert REVIEW_GENERATION_BUDGETS[prompt_id] > 0


def test_review_export_always_emits_markdown_and_docx(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(manuscript_export, "_EXPORT_DIR", tmp_path)
    session = ChatSession(topic="Unicode 综述")
    session.literature_review = "# 引言\n\n**加粗** 与 [链接](https://example.org)。\n\n# 结论\n\n正文。\n"
    files = write_reports(session, {"write_review"})
    assert {item["fileType"] for item in files} == {"text", "word"}
    md = next(tmp_path / item["fileName"] for item in files if item["fileName"].endswith(".md"))
    docx = next(tmp_path / item["fileName"] for item in files if item["fileName"].endswith(".docx"))
    assert "# 引言" in md.read_text(encoding="utf-8")
    assert docx.read_bytes()[:2] == b"PK"
    with zipfile.ZipFile(docx) as archive:
        assert archive.testzip() is None


def test_upload_digest_covers_every_parent_chunk_and_reuses_cache(monkeypatch):
    import agents.review_agent as review

    calls: list[str] = []

    async def fake_call(prompt_id: str, **kwargs):
        assert prompt_id == "review.upload_chunk"
        calls.append(kwargs["chunk_text"])
        return f"归纳::{kwargs['chunk_text']}"

    monkeypatch.setattr(review, "_call_prompt", fake_call)
    evidence = [{
        "id": "upload:a1", "attachment_id": "a1", "kind": "upload",
        "filename": "全篇.txt", "doc_fingerprint": "fp",
        "chunks": [
            {"parent_id": "p0", "section": "引言", "parent_text": "HEAD_MARKER"},
            {"parent_id": "p1", "section": "方法", "parent_text": "MIDDLE_MARKER"},
            {"parent_id": "p2", "section": "结论", "parent_text": "TAIL_MARKER"},
            # Duplicate child of p2 must not cause a duplicate summary call.
            {"parent_id": "p2", "section": "结论", "parent_text": "TAIL_MARKER"},
        ],
    }]
    state = {"attachment_texts": {"a1": "test-only"}}
    asyncio.run(_prepare_upload_digests(evidence, state))
    assert calls == ["HEAD_MARKER", "MIDDLE_MARKER", "TAIL_MARKER"]
    digest = evidence[0]["structured_digest"]
    assert all(marker in digest for marker in ("HEAD_MARKER", "MIDDLE_MARKER", "TAIL_MARKER"))

    calls.clear()
    asyncio.run(_prepare_upload_digests(evidence, state))
    assert calls == []


def test_unknown_citations_are_rejected_but_markdown_links_survive():
    text = _validate_citations(
        "有效 [p]；伪造 [zzz]；排除 [missing]；[站点](https://example.org)。",
        {"p"}, {"missing"},
    )
    assert "[p]" in text
    assert "[zzz]" not in text
    assert text.count("[证据不足]") == 2
    assert "[站点](https://example.org)" in text


def test_no_evidence_review_keeps_all_fixed_sections():
    result = asyncio.run(review_agent({
        "topic": "无证据主题", "papers": [_paper("empty", "")], "attachments": [],
    }))
    review = result["literature_review"]
    headings = (
        "# 引言", "# 综述范围与证据基础", "# 分类框架与分主题综述",
        "# 方法、发现与发展脉络的综合比较", "# 主要争议、共同局限与研究空白",
        "# 未来研究方向", "# 结论", "# 纳入文献与证据层级说明",
    )
    assert all(heading in review for heading in headings)
    assert "纳入 0 条证据" in review
    assert "标题 empty" not in review


def test_short_revision_triggers_evidence_bounded_expansion(monkeypatch):
    import agents.review_agent as review

    calls: list[str] = []
    headings = (
        "# 引言", "# 综述范围与证据基础", "# 分类框架与分主题综述",
        "# 方法、发现与发展脉络的综合比较", "# 主要争议、共同局限与研究空白",
        "# 未来研究方向", "# 结论", "# 纳入文献与证据层级说明",
    )

    async def fake_call(prompt_id: str, **kwargs):
        calls.append(prompt_id)
        if prompt_id == "review.theme":
            return "摘要报告了研究问题与方法 [p]。"
        if prompt_id == "review.revision":
            return "\n\n".join(f"{h}\n\n短段 [p]。" for h in headings)
        if prompt_id == "review.expansion":
            body = "摘要明确报告的方法与发现之间存在可比较关系 [p]。" * 15
            return "\n\n".join(f"{h}\n\n{body}" for h in headings)
        return ""

    monkeypatch.setattr(review, "_call_prompt", fake_call)
    result = asyncio.run(review_agent({
        "topic": "扩写测试", "papers": [_paper("p", "摘要明确报告了方法与发现。")],
        "attachments": [], "target_length": 3000,
    }))
    assert "review.expansion" in calls
    assert result["review_stats"]["actual_length"] > 1000
    assert result["review_stats"]["target_length"] == 3000
    assert "标题 p" in result["literature_review"]


def test_write_review_lazily_prepares_selected_upload(monkeypatch):
    import agents.review_agent as review
    import agents.tools_impl as impl
    import tools.export.report as report
    import tools.ingest.attachments as attachments

    prepared: list[str] = []

    async def fake_understand(attachment, session, *, focus=""):
        prepared.append(str(attachment["id"]))
        return {"id": attachment["id"], "status": "text_only", "element_count": 0}

    async def fake_review(state, progress_callback=None):
        return {
            "literature_review": "# 引言\n\n正文\n",
            "review_stats": {"included_count": 1, "excluded_count": 0, "target_length": 1800},
            "review_evidence": [], "review_exclusions": [],
        }

    monkeypatch.setattr(attachments, "ensure_attachment_understood", fake_understand)
    monkeypatch.setattr(review, "review_agent", fake_review)
    monkeypatch.setattr(report, "write_reports", lambda session, kinds: [])
    session = ChatSession(topic="上传综述", attachments=[{
        "id": "a1", "filename": "论文.txt", "ext": "txt", "media_type": "text/plain",
    }])
    result = asyncio.run(impl._tool_write_review({}, session, None))
    assert result.status == "success"
    assert prepared == ["a1"]


def test_explicit_concise_focus_is_the_only_short_review_path():
    target, note = _minimum_target(8, 1, 500, "请做简要概述")
    assert target == 1200
    assert "短版" in note
    normal_target, _ = _minimum_target(8, 1, 500, "方法比较")
    assert normal_target >= 3000
