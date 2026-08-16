"""Portable report artifacts for self-hosted UI and 清小搭 attachments."""
from __future__ import annotations

from pathlib import Path

from agents.session import ChatSession
from tools.export import report


def _map_session() -> ChatSession:
    session = ChatSession(topic="图神经网络")
    session.map_data = {
        "landscape": "从谱方法发展到消息传递。",
        "clusters": [{
            "id": 0, "label": "基础方法", "overview": "核心思想",
            "papers": [{"id": "p1", "title": "Paper One", "year": 2018,
                        "citation_count": 10}],
        }],
        "timeline": [{"year": 2018, "papers": [{"title": "Paper One"}]}],
        "graph": {
            "nodes": [
                {"id": "p1", "title": "Paper One", "year": 2018,
                 "citation_count": 10, "cluster": 0, "role": "foundational"},
                {"id": "p2", "title": "Paper Two", "year": 2020,
                 "citation_count": 4, "cluster": 0, "role": "bridge"},
            ],
            "edges": [{"source": "p1", "target": "p2", "type": "cites", "weight": 1}],
        },
    }
    return session


def test_research_map_svg_is_portable_and_escaped():
    session = _map_session()
    session.topic = "A & B <graph>"
    svg = report.render_research_map_svg(session)
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert "A &amp; B &lt;graph&gt;" in svg
    assert "Paper One" in svg and "Paper Two" in svg
    assert "<line" in svg and "<circle" in svg


def test_write_reports_emits_markdown_and_svg(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "_EXPORT_DIR", tmp_path)
    records = report.write_reports(_map_session(), {"research_map"})
    assert {r["mimeType"] for r in records} == {"text/markdown", "image/svg+xml"}
    assert {r["fileType"] for r in records} == {"text", "image"}
    assert all(Path(r["path"]).is_file() and r["size"] > 0 for r in records)
