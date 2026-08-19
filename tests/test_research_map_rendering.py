"""Pretty research-map SVG/Markdown rendering and strategy selection."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from agents.session import ChatSession
from core.models import Paper
from tools.export.report import (
    render_pretty_research_map_markdown,
    render_pretty_research_map_svg,
    render_research_map_svg,
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
        nodes.append({
            "id": f"d{index}", "title": f"Dense {index}", "year": 2024,
            "citation_count": index, "cluster": 2, "role": "", "layer": "core",
        })
    # Exercise long time spans and too many theme clusters without changing
    # the fixed SVG coordinate width.
    for index in range(12):
        nodes.append({
            "id": f"wide{index}", "title": f"Wide {index}", "year": 1800 + index * 20,
            "citation_count": 0, "cluster": index + 3, "role": "", "layer": "core",
        })
    session = ChatSession(topic='图谱 <主题> & "安全"')
    session.papers = [
        Paper(id="p1", title=title, year=1998, doi="10.1000/a&b",
              urls={"doi": "https://doi.org/10.1000/a%26b"}),
        Paper(id="p2", title="Bridge Paper", year=2024,
              urls={"source": "https://example.org/p2"}),
    ]
    session.map_data = {
        "clusters": [
            {"id": index, "label": f"主题 <{index}> & cluster", "overview": f"摘要 {index}"}
            for index in range(15)
        ],
        "timeline": [],
        "landscape": "",
        "graph": {
            "nodes": nodes,
            "edges": [
                {"source": "p2", "target": "p1", "type": "cites"},
                {"source": "p3", "target": "p1", "type": "semantic"},
                {"source": "d0", "target": "p1", "type": "cites"},
            ],
        },
    }
    return session


def test_pretty_svg_is_safe_scalable_and_complete():
    svg = render_pretty_research_map_svg(_session(hostile=True))
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.attrib["width"] == "100%"
    assert root.attrib["height"] == "auto"
    assert root.attrib["preserveAspectRatio"] == "xMidYMid meet"
    assert root.attrib["viewBox"].startswith("0 0 1240 ")
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert "javascript:" not in svg.lower()
    assert "http://" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "https://" not in svg
    assert "href=" not in svg and "<image" not in svg
    assert "直接引用" in svg and "语义关联" in svg
    assert "奠基性论文" in svg and "聚合节点" in svg
    assert "arrow-cite" in svg and "stroke-dasharray" in svg
    assert "+4" in svg
    assert "其他主题" in svg
    assert "1998" in svg and "n.d." in svg


def test_pretty_svg_empty_single_node_and_legacy_are_independent():
    empty = ChatSession(map_data={})
    assert render_pretty_research_map_svg(empty) == ""

    one = ChatSession(topic="one", map_data={
        "clusters": [], "graph": {"nodes": [
            {"id": "one", "title": "Only", "year": None, "cluster": 0,
             "citation_count": 0, "role": "", "layer": "core"},
        ], "edges": []},
    })
    ET.fromstring(render_pretty_research_map_svg(one))
    legacy = render_research_map_svg(one)
    assert legacy.startswith("<svg")
    assert 'preserveAspectRatio="xMidYMid meet"' not in legacy


def test_pretty_markdown_is_plain_relation_listing_without_mermaid():
    markdown = render_pretty_research_map_markdown(_session())
    assert "```mermaid" not in markdown.lower()
    assert "## 论文清单" in markdown
    assert "## 引用与语义关系" in markdown
    assert "## 聚合节点展开" in markdown
    assert "直接引用" in markdown and "语义关联" in markdown
    assert "DOI: 10.1000/a&b" in markdown
    assert "https://doi.org/10.1000/a%26b" in markdown
    assert "SVG 是主图形附件" in markdown


def test_write_reports_selects_strategy(monkeypatch, tmp_path: Path):
    import tools.export.report as report

    monkeypatch.setattr(report, "_EXPORT_DIR", tmp_path)
    session = _session()

    legacy = write_reports(session, {"research_map"}, research_map_render_strategy="legacy_svg")
    assert [item["mimeType"] for item in legacy] == ["text/markdown", "image/svg+xml"]
    assert "研究谱系 ·" in Path(legacy[1]["path"]).read_text(encoding="utf-8")

    pretty = write_reports(session, {"research_map"}, research_map_render_strategy="pretty_svg")
    assert [item["mimeType"] for item in pretty] == ["image/svg+xml"]
    assert 'preserveAspectRatio="xMidYMid meet"' in Path(pretty[0]["path"]).read_text(encoding="utf-8")

    paired = write_reports(
        session, {"research_map"}, research_map_render_strategy="pretty_svg_markdown",
    )
    assert [item["mimeType"] for item in paired] == ["text/markdown", "image/svg+xml"]
    assert "## 引用与语义关系" in Path(paired[0]["path"]).read_text(encoding="utf-8")
