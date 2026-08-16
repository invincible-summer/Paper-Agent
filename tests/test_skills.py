"""Tests for the skill layer: registry loader, use_skill dispatch,
executable export skills, and the system-prompt skill section.
"""
from __future__ import annotations

import pytest

from agents.orchestrator import _build_tool_result_message
from agents.session import ChatSession
from agents.tools_impl import (
    _tool_citation_export,
    _tool_export_report,
    _tool_use_skill,
)
from core.models import Paper
from core.prompts.registry import get as get_prompt
from core.skills import all_skills, get_skill, load_skills, skills_prompt_section
from core.tool_protocol import ErrorCode


def _paper(**kw):
    base = dict(id="p1", title="Graph Neural Networks for Rec", authors=["Smith, John"],
                year=2023, doi="10.1/test")
    base.update(kw)
    return Paper(**base)


def _session_with_papers() -> ChatSession:
    return ChatSession(session_id="t-skills", papers=[_paper(), _paper(id="p2", title="Another Paper")])


@pytest.fixture(autouse=True)
def _no_enrich_network(monkeypatch):
    """Citation export must not hit Crossref in tests."""
    import tools.export.enrich as enr

    async def fake(dois):
        return {}

    monkeypatch.setattr(enr, "enrich_by_dois", fake)


# --- loader ---------------------------------------------------------------

def test_load_skills_finds_builtins():
    load_skills()
    names = {s.name for s in all_skills()}
    assert {"compare_papers", "research_gap"} <= names
    for s in all_skills():
        assert s.description  # dispatch quality depends on it
        assert s.version >= 1


def test_skill_instructions_registered_in_prompt_registry():
    load_skills()
    p = get_prompt("skill.compare_papers")
    assert p.version >= 1
    assert "对比" in p.text or "对比表" in p.text


def test_malformed_skill_skipped(tmp_path):
    from core.skills import _parse_skill

    bad = tmp_path / "SKILL.md"
    bad.write_text("no frontmatter here", encoding="utf-8")
    assert _parse_skill(bad) is None

    bad2 = tmp_path / "SKILL2.md"
    bad2.write_text("---\nname: x\n---\n", encoding="utf-8")  # no description/body
    assert _parse_skill(bad2) is None


def test_skills_prompt_section_lists_metadata_only():
    load_skills()
    section = skills_prompt_section()
    assert "compare_papers" in section and "research_gap" in section
    assert "use_skill" in section
    # Metadata only: the full instructions body must NOT be in the section.
    assert "执行步骤" not in section


def test_system_prompt_includes_skill_section():
    import core.prompts  # noqa: F401
    from core.prompts.system import get_system_prompt

    text = get_system_prompt()
    assert "技能（按需加载）" in text
    assert "compare_papers" in text


# --- frontmatter v2: requires / output_check / focus ---------------------------

def test_frontmatter_requires_and_output_check():
    load_skills()
    skills = {s.name: s for s in all_skills()}
    assert skills["compare_papers"].requires == ("papers",)
    assert skills["compare_papers"].output_check  # self-check contract present
    assert skills["draft_review"].requires == ("attachments",)


def test_legacy_requires_papers_maps_to_tuple(tmp_path):
    from core.skills import _parse_skill

    md = tmp_path / "SKILL.md"
    md.write_text("---\nname: legacy_demo\ndescription: demo\nrequires_papers: true\n---\n\nbody\n",
                  encoding="utf-8")
    sk = _parse_skill(md)
    assert sk is not None and sk.requires == ("papers",)


@pytest.mark.anyio
async def test_use_skill_focus_and_output_check_injected():
    load_skills()
    session = _session_with_papers()
    r = await _tool_use_skill({"name": "compare_papers", "focus": "只对比方法"}, session, None)
    assert not r.is_error
    assert "用户焦点：只对比方法" in r.data["instructions"]
    assert "完成前自检" in r.data["instructions"]


@pytest.mark.anyio
async def test_use_skill_attachments_gate():
    load_skills()
    r = await _tool_use_skill({"name": "draft_review"}, _session_with_papers(), None)
    assert r.is_error and r.error_code == ErrorCode.NO_PAPERS
    assert "上传" in r.error["message"]

    session = _session_with_papers()
    session.attachments.append({"id": "a1", "filename": "draft.pdf"})
    r2 = await _tool_use_skill({"name": "draft_review"}, session, None)
    assert not r2.is_error


def test_suggest_skills_registry_checked():
    from agents.tools_impl import suggest_skills

    load_skills()
    session = _session_with_papers()
    hint = suggest_skills("search_papers", session)
    assert hint and "compare_papers" in hint
    # already-loaded skills are not re-suggested
    session.loaded_skills.add("compare_papers")
    hint2 = suggest_skills("search_papers", session)
    assert hint2 is None or "compare_papers" not in hint2
    # tools without hints return None
    assert suggest_skills("ask_papers", session) is None


# --- use_skill dispatch -----------------------------------------------------

@pytest.mark.anyio
async def test_use_skill_loads_instructions_once():
    load_skills()
    session = _session_with_papers()
    r1 = await _tool_use_skill({"name": "compare_papers"}, session, None)
    assert not r1.is_error
    assert "执行步骤" in r1.data["instructions"]
    # The full instructions are fed back into the LLM context message.
    msg = _build_tool_result_message("use_skill", r1)
    assert "技能指令" in msg and "执行步骤" in msg

    r2 = await _tool_use_skill({"name": "compare_papers"}, session, None)
    assert not r2.is_error
    assert r2.data["instructions"] == ""  # dedup: no token re-spend
    assert "已加载" in r2.text


@pytest.mark.anyio
async def test_use_skill_unknown_name():
    load_skills()
    r = await _tool_use_skill({"name": "fly_to_moon"}, _session_with_papers(), None)
    assert r.is_error and r.error_code == ErrorCode.NO_TOOL
    assert "compare_papers" in r.error["message"]  # available list in the message


@pytest.mark.anyio
async def test_use_skill_requires_papers_gate():
    load_skills()
    r = await _tool_use_skill({"name": "compare_papers"}, ChatSession(session_id="t-empty"), None)
    assert r.is_error and r.error_code == ErrorCode.NO_PAPERS
    assert "search_papers" in r.error["message"]


# --- citation_export -----------------------------------------------------------

@pytest.mark.anyio
async def test_citation_export_defaults_to_core_set():
    session = _session_with_papers()
    session.candidates.append(_paper(id="p3", title="Candidate Paper"))
    r = await _tool_citation_export({}, session, None)
    assert not r.is_error
    assert r.data["count"] == 2  # core only, candidates excluded
    assert r.data["format"] == "bibtex"
    assert r.data["citations"].count("@") == 2


@pytest.mark.anyio
async def test_citation_export_gbt7714():
    r = await _tool_citation_export({"format": "gbt7714"}, _session_with_papers(), None)
    assert not r.is_error
    assert r.data["citations"].startswith("[1]")
    assert "[J]" in r.data["citations"] or "[EB/OL]" in r.data["citations"]


@pytest.mark.anyio
async def test_citation_export_selected_and_bad_ids():
    session = _session_with_papers()
    r = await _tool_citation_export({"paper_ids": ["p2"]}, session, None)
    assert not r.is_error and r.data["count"] == 1

    r2 = await _tool_citation_export({"paper_ids": ["nope"]}, session, None)
    assert r2.is_error and r2.error_code == ErrorCode.VALIDATION_ERROR


@pytest.mark.anyio
async def test_citation_export_no_papers():
    r = await _tool_citation_export({}, ChatSession(session_id="t-empty2"), None)
    assert r.is_error and r.error_code == ErrorCode.NO_PAPERS


# --- export_report ------------------------------------------------------------

@pytest.mark.anyio
async def test_export_report_partial_when_nothing():
    r = await _tool_export_report({"kind": "all"}, _session_with_papers(), None)
    assert r.status == "partial"

    r2 = await _tool_export_report({"kind": "write_review"}, _session_with_papers(), None)
    assert r2.is_error and r2.error_code == ErrorCode.NO_PAPERS


@pytest.mark.anyio
async def test_export_report_writes_files_with_urls(tmp_path, monkeypatch):
    import tools.export.report as report_mod

    monkeypatch.setattr(report_mod, "_EXPORT_DIR", tmp_path)
    session = _session_with_papers()
    session.topic = "GNN 推荐"
    session.map_data = {"landscape": "领域脉络段落", "clusters": [], "timeline": []}
    r = await _tool_export_report({"kind": "research_map"}, session, None)
    assert not r.is_error
    files = r.data["files"]
    assert len(files) == 1
    assert files[0]["url"].startswith("/files/")
    written = tmp_path / files[0]["fileName"]
    assert written.exists() and "领域脉络段落" in written.read_text(encoding="utf-8")

    msg = _build_tool_result_message("export_report", r)
    assert "下载链接" in msg and "/files/" in msg
