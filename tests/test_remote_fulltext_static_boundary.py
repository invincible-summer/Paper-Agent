"""Static regressions for the retired network-paper full-text surface."""
from __future__ import annotations

from pathlib import Path

from agents.chat_tools import DeepReadArgs


ROOT = Path(__file__).resolve().parents[1]


def _read(paths: list[Path]) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_deep_read_schema_exposes_upload_ids_only():
    fields = set(DeepReadArgs.model_fields)
    assert fields == {"attachment_ids", "focus"}
    assert "paper_ids" not in DeepReadArgs.__doc__


def test_runtime_dispatch_has_no_remote_fetch_or_probe_path():
    runtime = _read([
        ROOT / "agents" / "search_agent.py",
        ROOT / "agents" / "tools_impl.py",
        ROOT / "agents" / "reader_agent.py",
        ROOT / "tools" / "search" / "manager.py",
    ]).casefold()
    forbidden = (
        "probe_pdf_url(", "pdffetcher(", "fetch_arxiv_pdf(",
        "verify_paper_fulltext(", "_ensure_fulltext(", "unpaywall",
    )
    assert not [token for token in forbidden if token in runtime]


def test_search_adapters_do_not_request_document_candidate_fields():
    adapters = _read(sorted((ROOT / "tools" / "search").glob("*.py")))
    assert "openAccessPdf" not in adapters
    assert "fileMain_s" not in adapters
    assert "best_oa_location" not in adapters


def test_prompts_and_skills_never_instruct_network_fulltext_acquisition():
    prompt_paths = sorted((ROOT / "core" / "prompts").glob("*.py"))
    skill_paths = sorted((ROOT / "skills" / "builtin").glob("*/SKILL.md"))
    text = _read(prompt_paths + skill_paths)
    forbidden = (
        "deep_read(paper_ids", "deep_read（paper_ids", "_ensure_fulltext",
        "自动获取网络论文全文", "自动下载论文全文", "应调用网络全文",
    )
    assert not [token for token in forbidden if token in text]
    # Positive boundary makes absence checks meaningful.
    assert "网络论文只" in text and "上传原文" in text


def test_public_admin_and_frontend_types_have_no_fulltext_policy_surface():
    paths = [
        ROOT / "backend" / "app" / "api" / "v1" / "admin.py",
        ROOT / "frontend" / "lib" / "admin-api.ts",
        ROOT / "frontend" / "lib" / "types.ts",
        ROOT / "frontend" / "app" / "admin" / "paper-search" / "page.tsx",
    ]
    text = _read(paths)
    forbidden = (
        "verify_fulltext", "fulltext_verify_timeout_seconds",
        "force_fulltext_probe", "paper_fetch_mode", "fulltext_core_count",
        "fulltext_status:",
    )
    assert not [token for token in forbidden if token in text]


def test_design_and_preview_do_not_describe_retired_fulltext_ui():
    text = _read([ROOT / "docs" / "DESIGN.md", ROOT / "scripts" / "preview_qxd_cards.py"])
    forbidden = (
        "exhibit_index(paper_ids", "fulltext_core_available",
        "full_text_paper_ids", "abstract_fallback_papers",
        "全文（🟢/🟡/⚪",
    )
    assert not [token for token in forbidden if token in text]
