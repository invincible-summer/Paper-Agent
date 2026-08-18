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
    ALL_CARD_TOOLS,
    CORE_CARD_TOOLS,
    MAX_CARD_CHARS,
    render_field_census_svg,
    render_skill_card,
    render_tool_card,
    resolve_full_card_tools,
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


# --- card renderers -------------------------------------------------------------


def test_search_card_lists_papers_with_fulltext_badges():
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
    card = render_tool_card("search_papers", result, CORE_CARD_TOOLS)
    assert "**🔎 文献检索 · 核心集 2 篇 / 候选 10 篇**" in card
    assert "🟢 **Paper A**（2021，被引 5）" in card
    assert "🟡" in card and "核心层全文保障 1/8" in card
    assert "PubMed/NLM 仅提供来源记录" in card


def test_search_card_truncates_long_lists():
    result = {
        "status": "success", "tool": "search_papers",
        "papers": [{"title": f"P{i}", "year": 2020, "citation_count": i,
                    "fulltext_status": "available"} for i in range(20)],
        "candidates": [],
    }
    card = render_tool_card("search_papers", result, CORE_CARD_TOOLS)
    assert "…等共 20 篇核心集论文" in card
    assert card.count("\n") < 20  # only top-8 listed


def test_research_map_card_summarizes_clusters_and_span():
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
    card = render_tool_card("research_map", result, CORE_CARD_TOOLS)
    assert "**🗺️ 研究地图 · 2 篇论文 / 2 个主题簇 / 1 条关联（引用 1）**" in card
    assert "**视觉基础**（2 篇）" in card and "2019–2024" in card
    assert "奠基性论文 1 篇" in card


def test_reading_path_card_roles_and_reasons():
    result = {"status": "success", "tool": "reading_path", "path": [
        {"paper_id": "a", "title": "Foundations", "role": "奠基",
         "reason": "领域开山之作，理解术语与问题定义"},
        {"paper_id": "b", "title": "Frontier", "role": "前沿", "reason": ""},
    ]}
    card = render_tool_card("reading_path", result, CORE_CARD_TOOLS)
    assert "【奠基】**Foundations**" in card and "领域开山之作" in card
    assert "【前沿】**Frontier**" in card


def test_deep_read_card_shows_structured_summary():
    result = {
        "status": "success", "tool": "deep_read",
        "full_text_paper_ids": ["a"], "abstract_fallback_papers": [{"paper_id": "b"}],
        "summaries": {
            "a": {"title": "Paper A", "research_problem": "如何高效压缩 KV 缓存",
                  "methodology": "分层量化", "key_findings": "显存降低 40%"},
            "b": {"title": "Paper B"},
        },
    }
    card = render_tool_card("deep_read", result, CORE_CARD_TOOLS)
    assert "**📖 深度阅读 · 1 篇全文级 / 1 篇摘要级**" in card
    assert "研究问题：如何高效压缩 KV 缓存" in card
    assert "主要发现：显存降低 40%" in card


def test_explain_element_cards_by_kind():
    figure = {"status": "success", "tool": "explain_element", "element": {
        "element_id": "p::figure::2", "kind": "figure", "page": 5,
        "caption": "Overall architecture",
        "understanding": {"description": "整体架构包含编码器与解码器。"},
        "docling_extract": {}}}
    card = render_tool_card("explain_element", figure, CORE_CARD_TOOLS)
    assert "🔍 图解读 · Overall architecture" in card and "整体架构" in card

    table = {"status": "success", "tool": "explain_element", "element": {
        "element_id": "p::table::1", "kind": "table",
        "docling_extract": {"markdown": "| a | b |\n|---|---|\n| 1 | 2 |"}}}
    card = render_tool_card("explain_element", table, CORE_CARD_TOOLS)
    assert "| a | b |" in card

    formula = {"status": "success", "tool": "explain_element", "element": {
        "element_id": "p::formula::1", "kind": "formula",
        "docling_extract": {"latex": "E = mc^2"}}}
    card = render_tool_card("explain_element", formula, CORE_CARD_TOOLS)
    assert "```latex" in card and "E = mc^2" in card


def test_field_census_and_write_review_and_citation_cards():
    census = {"status": "success", "tool": "field_census",
              "yearly": [{"key": 2023, "name": "2023", "count": 900},
                         {"key": 2024, "name": "2024", "count": 1200}],
              "top_authors": [{"name": "A", "count": 3}],
              "top_institutions": [], "top_venues": []}
    card = render_tool_card("field_census", census, CORE_CARD_TOOLS)
    assert "2023: 900 → 2024: 1200" in card and "高产作者：A（3）" in card

    review = {"status": "success", "tool": "write_review",
              "literature_review": "综述正文开头。", "review_chars": 5000}
    card = render_tool_card("write_review", review, CORE_CARD_TOOLS)
    assert "📝 文献综述已生成 · 5000 字" in card and "综述正文开头" in card

    cite = {"status": "success", "tool": "citation_export",
            "citations": "@article{a, title={T}}", "format": "bibtex", "count": 1}
    card = render_tool_card("citation_export", cite, CORE_CARD_TOOLS)
    assert "📑 参考文献导出 · BibTeX · 1 篇" in card and "@article{a" in card


def test_one_liner_for_non_core_and_errors():
    ok = {"status": "success", "tool": "integrity_sweep",
          "summary": "可靠性质检完成（12 篇）：无异常 10"}
    card = render_tool_card("integrity_sweep", ok, CORE_CARD_TOOLS)
    assert card == "**🛡️ 可靠性质检** · 可靠性质检完成（12 篇）：无异常 10"

    bad = {"status": "error", "tool": "integrity_sweep",
           "error": {"message": "网络受限"}}
    card = render_tool_card("integrity_sweep", bad, CORE_CARD_TOOLS)
    assert card.startswith("⚠️ 🛡️ 可靠性质检 · 网络受限")


def test_card_degrades_on_missing_payloads():
    empty = {"status": "success", "tool": "search_papers"}
    assert render_tool_card("search_papers", empty, CORE_CARD_TOOLS) == \
        "**🔎 文献检索**"
    # unknown tools never render a card
    assert render_tool_card("mystery", {"status": "success"}, CORE_CARD_TOOLS) is None
    # a broken payload must not raise
    assert render_tool_card("research_map", {"graph": None}, CORE_CARD_TOOLS) is not None


def test_card_respects_length_cap():
    result = {"status": "success", "tool": "citation_export",
              "citations": "x" * 10_000, "format": "bibtex", "count": 99}
    card = render_tool_card("citation_export", result, CORE_CARD_TOOLS)
    assert len(card) <= MAX_CARD_CHARS


def test_render_tool_card_custom_full_set_changes_card_depth():
    result = {"status": "success", "tool": "search_papers",
              "papers": [{"title": "T", "year": 2020, "citation_count": 1,
                          "fulltext_status": "available"}], "candidates": []}
    one_liner_set = CORE_CARD_TOOLS - {"search_papers"}
    assert "文献检索 · 核心集" not in render_tool_card(
        "search_papers", result, one_liner_set)


def test_skill_card_and_display_title():
    assert render_skill_card("研究空白识别与选题评估") == \
        "━━ 📘 技能 · 研究空白识别与选题评估 ━━"
    title = skill_display_title("research_gap")
    assert title == "研究空白识别与选题评估"
    assert skill_display_title("__missing__") == "__missing__"


def test_resolve_full_card_tools_presets():
    assert resolve_full_card_tools("all") == ALL_CARD_TOOLS
    assert resolve_full_card_tools("core") == CORE_CARD_TOOLS
    assert resolve_full_card_tools("off") == frozenset()
    custom = resolve_full_card_tools("custom", ["search_papers", "not_a_tool"])
    assert custom == {"search_papers"}


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
    assert policy.preset == "core" and policy.skill_card_enabled

    updated = store.update_display_policy(
        {"preset": "custom", "enabled_tools": ["search_papers", "deep_read"]},
        expected_version=1, updated_by="tester")
    assert updated.preset == "custom" and updated.version == 2
    assert set(updated.enabled_tools) == {"search_papers", "deep_read"}
    # round-trips through JSON
    assert set(store.get_display_policy().enabled_tools) == {"deep_read", "search_papers"}

    with pytest.raises(PolicyVersionConflict):
        store.update_display_policy({"preset": "all"}, expected_version=1,
                                    updated_by="tester")
    with pytest.raises(ValueError):
        store.update_display_policy({"preset": "nope"}, expected_version=2,
                                    updated_by="tester")
    with pytest.raises(ValueError):
        store.update_display_policy({"enabled_tools": ["ok", ""]},
                                    expected_version=2, updated_by="tester")


@pytest.mark.anyio
async def test_display_policy_admin_routes(client, monkeypatch):
    monkeypatch.setattr("app.api.v1.admin.current_user",
                        lambda *a, **k: {"id": "admin1", "role": "administrator"})
    got = await client.get("/api/v1/admin/display-policy")
    assert got.status_code == 200
    body = got.json()
    assert body["policy"]["preset"] == "core"
    assert "search_papers" in body["tools"]
    assert set(body["core_tools"]) == set(CORE_CARD_TOOLS)

    put = await client.put("/api/v1/admin/display-policy", json={
        "expected_version": body["policy"]["version"], "preset": "custom",
        "enabled_tools": ["research_map", "write_review"],
        "skill_card_enabled": False})
    assert put.status_code == 200
    updated = put.json()["policy"]
    assert updated["preset"] == "custom"
    assert updated["skill_card_enabled"] is False
    assert set(updated["enabled_tools"]) == {"research_map", "write_review"}

    conflict = await client.put("/api/v1/admin/display-policy", json={
        "expected_version": body["policy"]["version"], "preset": "all"})
    assert conflict.status_code == 409

    unknown = await client.put("/api/v1/admin/display-policy", json={
        "expected_version": updated["version"], "enabled_tools": ["nope"]})
    assert unknown.status_code == 422

    empty_custom = await client.put("/api/v1/admin/display-policy", json={
        "expected_version": updated["version"], "preset": "custom",
        "enabled_tools": []})
    assert empty_custom.status_code == 422
