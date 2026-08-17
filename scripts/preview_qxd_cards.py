#!/usr/bin/env python
"""Local visual preview of the 清小搭 (/v1) markdown cards and SVG artifacts.

Renders every card renderer + both SVG generators with representative
payloads into data/preview/ so the 清小搭-side look can be iterated on
without a deployment. Development-machine convenience only — never shipped
or deployed.

Usage:
    ./.env_conda/bin/python scripts/preview_qxd_cards.py
    # then open data/preview/qxd_cards_preview.html in a browser
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.export.cards import (  # noqa: E402
    ALL_CARD_TOOLS,
    CORE_CARD_TOOLS,
    render_field_census_svg,
    render_skill_card,
    render_tool_card,
    skill_display_title,
)
from tools.export.report import render_research_map_svg  # noqa: E402


def _papers(n, prefix, start_year=2018):
    return [
        {
            "id": f"{prefix}{i}",
            "title": f"{prefix.title()} Paper {i}: {['高效推理', '视觉语言对齐', '长上下文建模'][i % 3]}方法研究",
            "authors": [f"Author {chr(65 + i % 26)}"],
            "year": start_year + i,
            "citation_count": [12000, 3000, 800, 90, 12, 3][i % 6],
            "cluster": i % 3,
            "fulltext_status": ["available", "unknown", "unavailable"][i % 3],
            "layer": "core" if i % 4 else "candidate",
        }
        for i in range(n)
    ]


def _graph():
    nodes = []
    for i, p in enumerate(_papers(9, "p", 2016)):
        nodes.append(p | {
            "id": p["id"], "role": "foundational" if i in (0, 3) else ("bridge" if i == 5 else "")})
    for i in range(4):  # dense 2024 bucket in cluster 2 -> "+4" aggregate
        nodes.append({"id": f"hot{i}", "title": f"Hot Topic Paper {i}",
                      "year": 2024, "citation_count": 30 + i, "cluster": 2,
                      "role": "", "layer": "core"})
    edges = [
        {"source": "p1", "target": "p0", "type": "cites"},
        {"source": "p4", "target": "p0", "type": "cites"},
        {"source": "p5", "target": "p3", "type": "cites"},
        {"source": "p7", "target": "p0", "type": "semantic"},
    ]
    return nodes, edges


SAMPLES: list[tuple[str, dict]] = [
    ("skill", {"name": "research_gap"}),
    ("search_papers", {
        "status": "success", "tool": "search_papers",
        "papers": _papers(9, "p")[:8], "candidates": _papers(10, "c"),
        "fulltext_core_available": 5, "fulltext_core_target": 8,
        "summary": "检索完成：核心集 8 篇，候选 10 篇。"}),
    ("research_map", {
        "status": "success", "tool": "research_map",
        "clusters": [{"id": 0, "label": "视觉基础模型", "papers": [1, 2]},
                     {"id": 1, "label": "视觉语言对齐", "papers": [1]},
                     {"id": 2, "label": "高效推理", "papers": [1]}],
        "graph": dict(zip(("nodes", "edges"), _graph()))}),
    ("reading_path", {
        "status": "success", "tool": "reading_path",
        "path": [{"paper_id": "p0", "title": "Attention Is All You Need",
                  "role": "奠基", "reason": "领域奠基工作，建立问题定义与术语"},
                 {"paper_id": "p3", "title": "ViT: An Image is Worth 16x16 Words",
                  "role": "桥梁", "reason": "把 Transformer 引入视觉，连接两条线"},
                 {"paper_id": "p7", "title": "LLaVA: Visual Instruction Tuning",
                  "role": "前沿", "reason": "当前多模态指令跟随的代表作"}]}),
    ("deep_read", {
        "status": "success", "tool": "deep_read",
        "full_text_paper_ids": ["p0", "p3"],
        "abstract_fallback_papers": [{"paper_id": "p7"}],
        "summaries": {
            "p0": {"title": "Attention Is All You Need",
                   "research_problem": "RNN 序列建模无法并行，长距离依赖建模弱",
                   "methodology": "纯自注意力 + 位置编码的编码器-解码器",
                   "key_findings": "机器翻译 BLEU 提升，训练时间大幅缩短"},
            "p3": {"title": "ViT", "research_problem": "CNN 归纳偏置是否必要",
                   "methodology": "图像切分为 16x16 patch 序列输入标准 Transformer",
                   "key_findings": "大数据预训练下超越 CNN"}}}),
    ("explain_element", {
        "status": "success", "tool": "explain_element",
        "element": {"element_id": "p3::figure::2", "paper_id": "p3", "kind": "figure",
                    "page": 5, "caption": "Figure 2: Overall architecture of ViT",
                    "understanding": {"description":
                                      "该图展示了 ViT 的整体流程：输入图像被切分为固定大小的 patch 序列，"
                                      "经线性投影得到 token 嵌入，加上位置编码后送入标准 Transformer 编码器，"
                                      "最后取 [CLS] token 的输出经 MLP 头完成分类。"},
                    "docling_extract": {}}}),
    ("field_census", {
        "status": "success", "tool": "field_census",
        "yearly": [{"key": y, "name": str(y), "count": c} for y, c in
                   [(2016, 180), (2017, 320), (2018, 610), (2019, 900),
                    (2020, 1500), (2021, 2300), (2022, 3600), (2023, 5200), (2024, 6800)]],
        "top_authors": [{"name": "J. Smith", "count": 42}, {"name": "李华", "count": 31},
                        {"name": "A. Kumar", "count": 27}],
        "top_institutions": [{"name": "Tsinghua University", "count": 210},
                             {"name": "MIT CSAIL", "count": 180}],
        "top_venues": [{"name": "CVPR", "count": 500}]}),
    ("write_review", {
        "status": "success", "tool": "write_review",
        "literature_review": "多模态大模型的研究沿三条主线演进：视觉基础模型、视觉语言对齐与高效推理。"
                             "自 2017 年自注意力机制确立以来……", "review_chars": 4820}),
    ("citation_export", {
        "status": "success", "tool": "citation_export",
        "citations": "@article{vaswani2017attention,\n  title={Attention is all you need},\n"
                     "  author={Vaswani, Ashish},\n  journal={NeurIPS},\n  year={2017}\n}",
        "format": "bibtex", "count": 3}),
    ("integrity_sweep", {
        "status": "success", "tool": "integrity_sweep",
        "summary": "可靠性质检完成（12 篇）：✅ 无异常 10 · ⛔ 撤稿 1 · 🔁 预印本已发表 1"}),
    ("exhibit_index", {
        "status": "success", "tool": "exhibit_index",
        "summary": "图表导览完成：从 3 个文档提取 18 个元素。"}),
]


def main() -> None:
    out_dir = ROOT / "data" / "preview"
    out_dir.mkdir(parents=True, exist_ok=True)

    class _Session:
        topic = "多模态大模型的高效推理"
        map_data = {"graph": dict(zip(("nodes", "edges"), _graph())),
                    "clusters": [{"id": 0, "label": "视觉基础模型"},
                                 {"id": 1, "label": "视觉语言对齐"},
                                 {"id": 2, "label": "高效推理"}]}

    blocks: list[str] = ["# 清小搭卡片预览（/v1 Markdown 卡片仿真）", ""]
    for key, payload in SAMPLES:
        if key == "skill":
            blocks.append(render_skill_card(skill_display_title(payload["name"])))
            blocks.append("")
            continue
        card = render_tool_card(key, payload, CORE_CARD_TOOLS)
        blocks.append(card or f"(no card for {key})")
        blocks.append("")

    md = "\n\n---\n\n".join(blocks)
    (out_dir / "qxd_cards_preview.md").write_text(md, encoding="utf-8")

    (out_dir / "research_graph_preview.svg").write_text(
        render_research_map_svg(_Session()), encoding="utf-8")
    census_payload = next(p for k, p in SAMPLES if k == "field_census")
    (out_dir / "field_census_preview.svg").write_text(
        render_field_census_svg(census_payload, title="多模态大模型的高效推理"),
        encoding="utf-8")

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>清小搭卡片预览</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>body{{max-width:860px;margin:24px auto;padding:0 16px;
font-family:system-ui,"Noto Sans CJK SC",sans-serif;line-height:1.65}}
img{{max-width:100%}} hr{{margin:28px 0}}</style></head><body>
<div id="cards"></div>
<h2>研究图谱 SVG（附件卡片效果）</h2><img src="research_graph_preview.svg">
<h2>领域普查 SVG（附件卡片效果）</h2><img src="field_census_preview.svg">
<script>
fetch('qxd_cards_preview.md').then(r=>r.text()).then(t=>{{
  document.getElementById('cards').innerHTML = marked.parse(t);
}});
</script></body></html>"""
    (out_dir / "qxd_cards_preview.html").write_text(html, encoding="utf-8")
    print(f"preview written to {out_dir}/  (open qxd_cards_preview.html)")


if __name__ == "__main__":
    main()
