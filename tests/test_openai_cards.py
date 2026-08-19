"""Tests for the /v1 markdown-card emulation layer (tools/export/cards.py),
the upgraded research-map SVG, and the admin display policy.

All offline pure-function tests; admin route tests stub current_user.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from tools.export.cards import (  # noqa: E402
    MAX_CARD_CHARS,
    render_field_census_svg,
    render_search_table,
    render_skill_card,
    render_tool_card,
    skill_display_title,
)
from tools.export.report import render_research_map_svg  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    import httpx
    from app.main import create_app
    from core.api_checkpoint import _STORE_CACHE

    monkeypatch.setenv("AGENT_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_STORAGE_ROOT", str(tmp_path / "openai-api"))
    _STORE_CACHE.clear()
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# --- card renderers (one-line status cards) --------------------------------------


def test_search_card_is_a_single_status_line():
    result = {
        "status": "success", "tool": "search_papers",
        "papers": [
            {"title": "Paper A", "year": 2021, "citation_count": 5,
             "fulltext_status": "available"},
            {"title": "Paper B", "year": 2023, "citation_count": 0,
             "fulltext_status": "unknown"},
        ],
        "candidates": [{"title": "C"}] * 10,
        "fulltext_core_available": 1, "fulltext_core_target": 8,
        "source_notices": ["PubMed/NLM 仅提供来源记录，不代表内容背书。"],
    }
    card = render_tool_card("search_papers", result)
    assert card == "**🔎 文献检索 · 核心集 2 篇 / 候选 10 篇**"
    # the per-paper listing lives in render_search_table, never in the card
    assert "Paper A" not in card and "核心层全文保障" not in card


def test_render_search_table_lists_all_papers_with_links():
    result = {
        "status": "success", "tool": "search_papers",
        "papers": [
            {"title": "Paper A", "year": 2021, "citation_count": 5,
             "fulltext_status": "available", "doi": "10.1234/a",
             "pdf_url": "https://oa.example.com/a.pdf"},
            {"title": "Paper B", "year": None, "citation_count": None,
             "fulltext_status": "unknown", "urls": {"openalex": "https://openalex.org/w/1"}},
        ],
        "candidates": [
            {"title": "Cand | Pipe", "year": 2023, "citation_count": 0,
             "fulltext_status": "unavailable"},
        ],
    }
    table = render_search_table(result)
    assert table is not None
    lines = table.splitlines()
    assert lines[0] == "| 分层 | 标题 | 年份 | 被引 | 全文 | 链接 |"
    assert lines[1].startswith("| --- |")
    assert len(lines) == 5  # header + separator + every core and candidate row
    assert ("| 核心 | Paper A | 2021 | 5 | [🟢](https://oa.example.com/a.pdf) "
            "| [DOI](https://doi.org/10.1234/a) |") in lines
    assert "| 核心 | Paper B | — | — | 🟡 | [来源](https://openalex.org/w/1) |" in lines
    assert "| 候选 | Cand \\| Pipe | 2023 | 0 | ⚪ | — |" in lines


def test_render_search_table_truncates_and_escapes_titles():
    result = {
        "status": "success", "tool": "search_papers",
        "papers": [{"title": "T" * 80, "year": 2020, "citation_count": 1,
                    "fulltext_status": "available", "doi": "https://doi.org/10.1/x"}],
        "candidates": [],
    }
    table = render_search_table(result)
    assert table is not None
    row = table.splitlines()[2]
    assert f"{'T' * 60}…" in row and "TT" * 31 not in row
    assert "[DOI](https://doi.org/10.1/x)" in row  # full DOI URLs pass through


def test_render_search_table_degrades_without_papers():
    assert render_search_table({"papers": [], "candidates": []}) is None
    assert render_search_table({}) is None
    assert render_search_table({"papers": "junk", "candidates": None}) is None


def test_research_map_card_is_a_single_status_line():
    result = {
        "status": "success", "tool": "research_map",
        "clusters": [{"id": 0, "label": "视觉基础", "papers": [1, 2]},
                     {"id": 1, "label": "对齐", "papers": [1]}],
        "graph": {
            "nodes": [
                {"id": "a", "year": 2019, "cluster": 0, "role": "foundational"},
                {"id": "b", "year": 2024, "cluster": 1, "role": ""},
            ],
            "edges": [{"source": "a", "target": "b", "type": "cites"}],
        },
    }
    card = render_tool_card("research_map", result)
    assert card == "**🗺️ 研究地图 · 2 篇论文 / 2 个主题簇 / 1 条关联**"


def test_reading_path_card_is_a_single_status_line():
    result = {"status": "success", "tool": "reading_path", "path": [
        {"paper_id": "a", "title": "Foundations", "role": "奠基",
         "reason": "领域开山之作，理解术语与问题定义"},
        {"paper_id": "b", "title": "Frontier", "role": "前沿", "reason": ""},
    ]}
    assert render_tool_card("reading_path", result) == "**🧭 推荐阅读路径 · 2 篇**"


def test_deep_read_card_is_a_single_status_line():
    result = {
        "status": "success", "tool": "deep_read",
        "full_text_paper_ids": ["a"], "abstract_fallback_papers": [{"paper_id": "b"}],
        "summaries": {
            "a": {"title": "Paper A", "research_problem": "如何高效压缩 KV 缓存",
                  "methodology": "分层量化", "key_findings": "显存降低 40%"},
            "b": {"title": "Paper B"},
        },
    }
    card = render_tool_card("deep_read", result)
    assert card == "**📖 深度阅读 · 1 篇全文级 / 1 篇摘要级**"
    assert "研究问题" not in card  # details no longer duplicated in the card


def test_explain_element_cards_by_kind():
    figure = {"status": "success", "tool": "explain_element", "element": {
        "element_id": "p::figure::2", "kind": "figure", "page": 5,
        "caption": "Overall architecture",
        "understanding": {"description": "整体架构包含编码器与解码器。"},
        "docling_extract": {}}}
    assert render_tool_card("explain_element", figure) == \
        "**🔍 图解读 · Overall architecture**"

    table = {"status": "success", "tool": "explain_element", "element": {
        "element_id": "p::table::1", "kind": "table",
        "docling_extract": {"markdown": "| a | b |\n|---|---|\n| 1 | 2 |"}}}
    assert render_tool_card("explain_element", table) == "**🔍 表解读**"

    formula = {"status": "success", "tool": "explain_element", "element": {
        "element_id": "p::formula::1", "kind": "formula",
        "docling_extract": {"latex": "E = mc^2"}}}
    assert render_tool_card("explain_element", formula) == "**🔍 公式解读**"


def test_field_census_and_write_review_and_citation_cards():
    census = {"status": "success", "tool": "field_census",
              "yearly": [{"key": 2023, "name": "2023", "count": 900},
                         {"key": 2024, "name": "2024", "count": 1200}],
              "top_authors": [{"name": "A", "count": 3}],
              "top_institutions": [], "top_venues": []}
    assert render_tool_card("field_census", census) == "**📊 领域普查完成**"

    review = {"status": "success", "tool": "write_review",
              "literature_review": "综述正文开头。", "review_chars": 5000}
    assert render_tool_card("write_review", review) == "**📝 文献综述已生成 · 5000 字**"

    cite = {"status": "success", "tool": "citation_export",
            "citations": "@article{a, title={T}}", "format": "bibtex", "count": 1}
    assert render_tool_card("citation_export", cite) == "**📑 参考文献导出 · BibTeX · 1 篇**"


def test_one_liner_for_non_core_and_errors():
    ok = {"status": "success", "tool": "integrity_sweep",
          "summary": "可靠性质检完成（12 篇）：无异常 10"}
    card = render_tool_card("integrity_sweep", ok)
    assert card == "**🛡️ 可靠性质检** · 可靠性质检完成（12 篇）：无异常 10"

    bad = {"status": "error", "tool": "integrity_sweep",
           "error": {"message": "网络受限"}}
    card = render_tool_card("integrity_sweep", bad)
    assert card.startswith("⚠️ 🛡️ 可靠性质检 · 网络受限")

    partial = {"status": "partial", "tool": "search_papers",
               "papers": [{"title": "T", "year": 2020, "citation_count": 1}],
               "candidates": [], "summary": "部分来源超时"}
    assert render_tool_card("search_papers", partial).startswith("🟡**🔎 文献检索")


def test_card_degrades_on_missing_payloads():
    empty = {"status": "success", "tool": "search_papers"}
    assert render_tool_card("search_papers", empty) == "**🔎 文献检索**"
    # unknown tools never render a card
    assert render_tool_card("mystery", {"status": "success"}) is None
    # a broken payload must not raise
    assert render_tool_card("research_map", {"graph": None}) is not None


def test_card_respects_length_cap():
    result = {"status": "success", "tool": "citation_export",
              "citations": "x" * 10_000, "format": "bibtex", "count": 99}
    card = render_tool_card("citation_export", result)
    assert len(card) <= MAX_CARD_CHARS


def test_skill_card_and_display_title():
    assert render_skill_card("研究空白识别与选题评估") == \
        "━━ 📘 技能 · 研究空白识别与选题评估 ━━"
    title = skill_display_title("research_gap")
    assert title == "研究空白识别与选题评估"
    assert skill_display_title("__missing__") == "__missing__"


# --- field census SVG -----------------------------------------------------------


def test_field_census_svg_chart_contents():
    result = {
        "yearly": [{"key": y, "name": str(y), "count": c} for y, c in
                   [(2020, 100), (2021, 300), (2022, 900), (2023, 1500)]],
        "top_authors": [{"name": "J. Smith", "count": 42}],
        "top_institutions": [{"name": "MIT", "count": 210}],
    }
    svg = render_field_census_svg(result, title="高效推理")
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "<polyline" in svg and "高产作者" in svg and "高产机构" in svg
    assert "高效推理" in svg and "2023" in svg
    # names are HTML-escaped
    assert render_field_census_svg(
        {"yearly": [{"key": 2020, "name": "2020", "count": 1}],
         "top_authors": [{"name": "<x>&", "count": 1}]},
    ).count("&lt;x&gt;&amp;") == 1
    assert render_field_census_svg({"yearly": []}) == ""


# --- research map SVG ------------------------------------------------------------


class _FakeSession:
    def __init__(self, map_data, topic="研究主题"):
        self.topic = topic
        self.map_data = map_data


def _map_with_counts(n_2024_cluster2: int):
    nodes = [
        {"id": "p1", "title": "Foundational Work", "year": 2019,
         "citation_count": 800, "cluster": 0, "role": "foundational", "layer": "core"},
        {"id": "p2", "title": "Bridge Work", "year": 2021,
         "citation_count": 60, "cluster": 0, "role": "bridge", "layer": "core"},
        {"id": "p3", "title": "Low Cited", "year": 2022,
         "citation_count": 3, "cluster": 1, "role": "", "layer": "candidate"},
    ]
    for i in range(n_2024_cluster2):
        nodes.append({"id": f"q{i}", "title": f"Hot Topic {i}", "year": 2024,
                      "citation_count": 10 + i, "cluster": 2, "role": "",
                      "layer": "core"})
    edges = [{"source": "p2", "target": "p1", "type": "cites"},
             {"source": "p3", "target": "p1", "type": "semantic"}]
    return {"graph": {"nodes": nodes, "edges": edges},
            "clusters": [{"id": 0, "label": "基础模型"},
                         {"id": 1, "label": "候选方向"},
                         {"id": 2, "label": "热点"}]}


def test_research_map_svg_structure_and_legend():
    svg = render_research_map_svg(_FakeSession(_map_with_counts(2)))
    assert svg.startswith("<svg")
    assert "研究谱系 ·" in svg and "3 个主题簇" in svg
    assert "基础模型" in svg and "热点" in svg          # cluster lane labels
    assert "光环 = 奠基性论文" in svg and "引用关系" in svg  # legend
    assert "语义相似" in svg
    # foundational halo + citation-tier radii present
    assert 'opacity="0.22"' in svg
    assert 'r="13"' in svg and 'r="10"' in svg and 'r="7"' in svg


def test_research_map_svg_aggregates_dense_buckets():
    svg = render_research_map_svg(_FakeSession(_map_with_counts(5)))
    assert ">+5</text>" in svg          # 5 papers in one (cluster, year) bucket


def test_research_map_svg_escapes_titles():
    data = _map_with_counts(1)
    data["graph"]["nodes"][0]["title"] = "<b>注入&测试</b>"
    svg = render_research_map_svg(_FakeSession(data))
    assert "<b>注入" not in svg
    assert "&lt;b&gt;注入&amp;测试" in svg


def test_research_map_svg_empty_graph():
    assert render_research_map_svg(_FakeSession({})) == ""


def test_generated_svgs_are_valid_xml():
    """Both SVG generators must emit well-formed XML (a literal `<` in text
    content once broke the whole attachment in Chromium hosts)."""
    import xml.etree.ElementTree as ET

    ET.fromstring(render_research_map_svg(_FakeSession(_map_with_counts(2))))
    census = {
        "yearly": [{"key": 2023, "name": "2023", "count": 900},
                   {"key": 2024, "name": "2024", "count": 1200}],
        "top_authors": [{"name": "A<b>&", "count": 3}],
        "top_institutions": [], "top_venues": [],
    }
    ET.fromstring(render_field_census_svg(census))


# --- display policy store --------------------------------------------------------


def test_display_policy_store_roundtrip(tmp_path):
    from core.api_storage_store import ApiStorageStore, PolicyVersionConflict, SCHEMA_VERSION
    from core.storage_context import StorageContext

    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path))
    store.initialize()
    assert store.schema_version() == SCHEMA_VERSION
    policy = store.get_display_policy()
    assert policy.tool_cards_enabled and policy.skill_card_enabled
    assert policy.research_map_render_strategy == "legacy_svg"

    updated = store.update_display_policy(
        {"tool_cards_enabled": False, "research_map_render_strategy": "pretty_svg"},
        expected_version=1, updated_by="tester")
    assert updated.tool_cards_enabled is False and updated.version == 2
    assert updated.research_map_render_strategy == "pretty_svg"
    assert store.get_display_policy().tool_cards_enabled is False

    with pytest.raises(PolicyVersionConflict):
        store.update_display_policy({"skill_card_enabled": False},
                                    expected_version=1, updated_by="tester")
    with pytest.raises(ValueError):
        store.update_display_policy({"tool_cards_enabled": "yes"},
                                    expected_version=2, updated_by="tester")
    with pytest.raises(ValueError):
        store.update_display_policy({"nope": 1}, expected_version=2, updated_by="tester")
    for invalid in ("", "mermaid", 1, None):
        with pytest.raises(ValueError):
            store.update_display_policy(
                {"research_map_render_strategy": invalid},
                expected_version=2, updated_by="tester",
            )


def test_display_policy_cache_expires_and_admin_update_invalidates(tmp_path, monkeypatch):
    import core.api_storage_store as storage
    from core.api_storage_store import ApiStorageStore
    from core.storage_context import StorageContext

    clock = [100.0]
    monkeypatch.setattr(storage.time, "monotonic", lambda: clock[0])
    storage.reset_display_policy_cache()
    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path))
    store.initialize()
    first = store.get_display_policy()

    with store.connect() as conn:
        conn.execute(
            "UPDATE api_display_policy SET research_map_render_strategy='pretty_svg' "
            "WHERE id=1"
        )
        conn.commit()
    assert store.get_display_policy() is first
    clock[0] += storage.DISPLAY_POLICY_CACHE_TTL_SECONDS + 0.1
    assert store.get_display_policy().research_map_render_strategy == "pretty_svg"

    current = store.get_display_policy()
    updated = store.update_display_policy(
        {"research_map_render_strategy": "pretty_svg_markdown"},
        expected_version=current.version, updated_by="admin",
    )
    assert updated.research_map_render_strategy == "pretty_svg_markdown"
    assert store.get_display_policy().research_map_render_strategy == "pretty_svg_markdown"


@pytest.mark.anyio
async def test_display_policy_admin_routes(client, monkeypatch):
    monkeypatch.setattr("app.api.v1.admin.current_user",
                        lambda *a, **k: {"id": "admin1", "role": "administrator"})
    got = await client.get("/api/v1/admin/display-policy")
    assert got.status_code == 200
    body = got.json()
    assert body["policy"]["tool_cards_enabled"] is True
    assert body["policy"]["skill_card_enabled"] is True
    assert body["policy"]["research_map_render_strategy"] == "legacy_svg"
    assert "tools" not in body and "core_tools" not in body

    put = await client.put("/api/v1/admin/display-policy", json={
        "expected_version": body["policy"]["version"],
        "tool_cards_enabled": False, "skill_card_enabled": False,
        "research_map_render_strategy": "pretty_svg_markdown"})
    assert put.status_code == 200
    updated = put.json()["policy"]
    assert updated["tool_cards_enabled"] is False
    assert updated["skill_card_enabled"] is False
    assert updated["research_map_render_strategy"] == "pretty_svg_markdown"

    conflict = await client.put("/api/v1/admin/display-policy", json={
        "expected_version": body["policy"]["version"], "tool_cards_enabled": True})
    assert conflict.status_code == 409

    bad_type = await client.put("/api/v1/admin/display-policy", json={
        "expected_version": updated["version"], "tool_cards_enabled": "yes"})
    assert bad_type.status_code == 422

    bad_strategy = await client.put("/api/v1/admin/display-policy", json={
        "expected_version": updated["version"],
        "research_map_render_strategy": "mermaid",
    })
    assert bad_strategy.status_code == 422

    extra = await client.put("/api/v1/admin/display-policy", json={
        "expected_version": updated["version"], "unknown": True,
    })
    assert extra.status_code == 422
