"""Deterministic multi-format research-map rendering."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from agents.session import ChatSession
from core.models import Paper
from tools.export.report import (
    build_research_map_view,
    render_pretty_research_map_markdown,
    render_pretty_research_map_svg,
    render_research_map_html,
    render_research_map_mermaid,
    write_reports,
)


def _session(*, dense: int = 4, hostile: bool = False) -> ChatSession:
    title = '中文 <script>alert("x")</script> & "引号"' if hostile else "Foundational Paper"
    nodes = [
        {"id": "p1", "title": title, "year": 1998, "citation_count": 900,
         "cluster": 0, "role": "foundational", "layer": "core"},
        {"id": "p2", "title": "Bridge Paper", "year": 2024, "citation_count": 80,
         "cluster": 1, "role": "bridge", "layer": "candidate"},
        {"id": "p3", "title": "No year", "year": None, "citation_count": 1,
         "cluster": 1, "role": "", "layer": "core"},
    ]
    for index in range(dense):
        nodes.append({"id": f"d{index}", "title": f"Dense {index}", "year": 2024,
                      "citation_count": index, "cluster": 2, "role": "", "layer": "core"})
    for index in range(12):
        nodes.append({"id": f"wide{index}", "title": f"Wide {index}",
                      "year": 1800 + index * 20, "citation_count": 0,
                      "cluster": index + 3, "role": "", "layer": "core"})
    session = ChatSession(topic='图谱 <主题> & "安全"')
    session.papers = [
        Paper(id="p1", title=title, year=1998, doi="10.1000/a&b",
              urls={"doi": "https://doi.org/10.1000/a%26b"}),
        Paper(id="p2", title="Bridge Paper", year=2024,
              urls={"source": "https://example.org/p2"}),
    ]
    session.map_data = {
        "clusters": [{"id": index, "label": f"主题 <{index}> & cluster", "overview": f"摘要 {index}"}
                      for index in range(15)],
        "timeline": [], "landscape": "",
        "graph": {"nodes": nodes, "edges": [
            {"source": "p2", "target": "p1", "type": "cites"},
            {"source": "p3", "target": "p1", "type": "semantic"},
            {"source": "d0", "target": "p1", "type": "cites"},
        ]},
    }
    return session


def test_shared_view_is_sorted_deduped_and_aggregated():
    view = build_research_map_view(_session())
    assert view["nodes"][0]["id"] == "p1"
    assert len(view["aggregates"]) == 1
    assert view["aggregates"][0]["member_ids"] == ["d0", "d1", "d2", "d3"]
    assert len(view["edges"]) == 3


def test_pretty_svg_is_safe_scalable_and_complete():
    svg = render_pretty_research_map_svg(_session(hostile=True))
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.attrib["width"] == "100%"
    assert root.attrib["height"] == "auto"
    assert root.attrib["preserveAspectRatio"] == "xMidYMid meet"
    assert "&lt;script&gt;" in svg and "javascript:" not in svg.lower()
    assert "<script>" not in svg and "href=" not in svg and "<image" not in svg
    assert "直接引用" in svg and "语义关联" in svg
    assert "奠基性论文" in svg and "聚合节点" in svg and "+4" in svg


def test_mermaid_is_deterministic_safe_and_has_distinct_edges():
    mermaid = render_research_map_mermaid(_session(hostile=True))
    assert mermaid == render_research_map_mermaid(_session(hostile=True))
    assert mermaid.startswith("```mermaid\nflowchart LR") and mermaid.endswith("```")
    assert "-->" in mermaid and "-. 语义 .->" in mermaid
    assert "click" not in mermaid.lower() and "<script>" not in mermaid.lower()
    assert "+4 篇" in mermaid
    assert "<script>alert" not in mermaid


def test_html_is_self_contained_and_interactive_without_external_resources():
    doc = render_research_map_html(_session(hostile=True))
    assert doc.startswith("<!doctype html>")
    assert "Content-Security-Policy" in doc and "connect-src 'none'" in doc
    assert "cluster-filter" in doc and "show-cites" in doc and "show-semantic" in doc
    assert "pointerdown" in doc and "wheel" in doc and "聚合成员" in doc
    assert "fetch(" not in doc and "localStorage" not in doc and "<script>alert" not in doc
    assert "noopener noreferrer" in doc and "target='_blank'" in doc
    assert "<script src=" not in doc and "<link rel=" not in doc and "<img" not in doc


def test_markdown_is_plain_relation_listing_without_mermaid():
    markdown = render_pretty_research_map_markdown(_session())
    assert "```mermaid" not in markdown.lower()
    assert "## 论文清单" in markdown and "## 引用与语义关系" in markdown
    assert "## 聚合节点展开" in markdown and "直接引用" in markdown and "语义关联" in markdown
    assert "DOI: 10.1000/a&b" in markdown


def test_write_reports_combinations_and_explicit_export_defaults(monkeypatch, tmp_path: Path):
    import tools.export.report as report
    monkeypatch.setattr(report, "_EXPORT_DIR", tmp_path)
    session = _session()
    records = write_reports(session, {"research_map"}, research_map_svg_enabled=True,
                            research_map_html_enabled=True, research_map_markdown_enabled=False)
    assert [r["mimeType"] for r in records] == ["image/svg+xml", "text/html"]
    assert all(Path(r["path"]).is_file() for r in records)
    explicit = write_reports(session, {"research_map"})
    assert [r["mimeType"] for r in explicit] == ["text/markdown", "image/svg+xml"]
