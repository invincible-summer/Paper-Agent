"""Markdown card emulation for the OpenAI-compatible (/v1) channel.

清小搭 (openai-compatible-agent-integration-guide.md, the only contract)
defines no structured content, HTML, WebView or tool-call display: the only
rich surfaces are ``delta.reasoning`` (thinking fold), ``delta.content``
(plain text — the model's own answers are already markdown) and
``x_soda.attachments`` (downloadable file cards).  These renderers emulate
the self-hosted frontend's tool cards as compact markdown blocks inserted
when a tool completes, before the final answer — mirroring the web layout
where cards sit above the reply.

Degradation-first style: bold headers and lists (readable even when the
host renders plain text), emoji icons mirroring the frontend TOOL_META,
top-N truncation, and a hard per-card length cap.  Every renderer is a
pure function over ``ToolResult.to_dict()`` payloads and must degrade to
None / a one-liner on missing fields — never raise.
"""
from __future__ import annotations

import html
import re
from typing import Any

# Mirrors frontend ChatMessage.tsx TOOL_META labels/icons.
_TOOL_META: dict[str, tuple[str, str]] = {
    "search_papers": ("🔎", "文献检索"),
    "deep_read": ("📖", "深度阅读"),
    "ask_papers": ("💬", "论文问答"),
    "research_map": ("🗺️", "研究地图"),
    "reading_path": ("🧭", "阅读路径"),
    "write_review": ("📝", "文献综述"),
    "citation_export": ("📑", "参考文献导出"),
    "export_report": ("📥", "报告导出"),
    "check_structure": ("📋", "结构体检"),
    "check_format": ("🧾", "格式检查"),
    "export_manuscript": ("📄", "文稿导出"),
    "integrity_sweep": ("🛡️", "可靠性质检"),
    "bib_import": ("📚", "文献库导入"),
    "exhibit_index": ("🖼️", "图表导览"),
    "explain_element": ("🔍", "元素解读"),
    "field_census": ("📊", "领域普查"),
    "use_skill": ("📘", "技能"),
}

# Default "core" display preset: these tools get full markdown cards,
# everything else gets a one-line summary card.
CORE_CARD_TOOLS: frozenset[str] = frozenset({
    "search_papers", "research_map", "reading_path", "deep_read",
    "explain_element", "field_census", "write_review", "citation_export",
})

ALL_CARD_TOOLS: frozenset[str] = frozenset(_TOOL_META) - {"use_skill"}


def resolve_full_card_tools(preset: str, enabled_tools=()) -> frozenset[str]:
    """Map an admin display-policy preset to the effective full-card set."""
    if preset == "all":
        return ALL_CARD_TOOLS
    if preset == "custom":
        return frozenset(str(t) for t in enabled_tools) & ALL_CARD_TOOLS
    if preset == "off":
        return frozenset()
    return CORE_CARD_TOOLS

MAX_CARD_CHARS = 1200
_SEARCH_TOP_N = 8
_DEEP_READ_TOP_N = 3
_PATH_TOP_N = 10

_FULLTEXT_EMOJI = {"available": "🟢", "unavailable": "⚪", "unknown": "🟡"}

_KIND_LABEL = {"figure": "图", "table": "表", "formula": "公式"}


def _short(text: Any, limit: int) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s[:limit] + ("…" if len(s) > limit else "")


def tool_label(tool: str) -> str:
    emoji, label = _TOOL_META.get(tool, ("✨", tool))
    return f"{emoji} {label}"


def _status_mark(result: dict) -> str:
    status = str(result.get("status") or "")
    if status == "error":
        return "⚠️"
    if status == "partial":
        return "🟡"
    return ""


def render_skill_card(title: str) -> str:
    """The in-content skill-loading line (清小搭 channel only)."""
    return f"━━ 📘 技能 · {_short(title, 40)} ━━"


def skill_display_title(name: str) -> str:
    """Human title for a skill: first sentence of its description, else name."""
    try:
        from core.skills import get_skill
        sk = get_skill(name)
        if sk is not None and sk.description:
            first = re.split(r"[。．\.]", sk.description.strip(), 1)[0]
            if first.strip():
                return first.strip()[:40]
    except Exception:  # noqa: BLE001 — display only, never fail a turn
        pass
    return name


# ---------------------------------------------------------------------------
# Full cards (core preset)
# ---------------------------------------------------------------------------

def _card_search_papers(result: dict) -> str:
    papers = [p for p in (result.get("papers") or []) if isinstance(p, dict)]
    candidates = result.get("candidates") or []
    if not papers and not candidates:
        return ""
    lines: list[str] = [
        f"**🔎 文献检索 · 核心集 {len(papers)} 篇 / 候选 {len(candidates)} 篇**", ""
    ]
    for i, p in enumerate(papers[:_SEARCH_TOP_N], 1):
        year = p.get("year") or "n.d."
        cites = p.get("citation_count") or 0
        badge = _FULLTEXT_EMOJI.get(str(p.get("fulltext_status") or ""), "🟡")
        lines.append(f"{i}. {badge} **{_short(p.get('title'), 80)}**（{year}，被引 {cites}）")
    if len(papers) > _SEARCH_TOP_N:
        lines.append(f"…等共 {len(papers)} 篇核心集论文")
    avail = result.get("fulltext_core_available")
    target = result.get("fulltext_core_target")
    legend = "🟢 全文可取 · 🟡 待验证 · ⚪ 仅摘要"
    if isinstance(avail, int) and isinstance(target, int):
        legend += f" · 核心层全文保障 {avail}/{target}"
    lines += ["", f"*{legend}*"]
    return "\n".join(lines)


def _card_research_map(result: dict) -> str:
    graph = result.get("graph") or {}
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    edges = graph.get("edges") or []
    clusters = [c for c in (result.get("clusters") or []) if isinstance(c, dict)]
    if not nodes and not clusters:
        return ""
    n_cite = sum(1 for e in edges if isinstance(e, dict) and e.get("type") == "cites")
    lines = [f"**🗺️ 研究地图 · {len(nodes)} 篇论文 / {len(clusters)} 个主题簇 / "
             f"{len(edges)} 条关联（引用 {n_cite}）**", ""]
    if clusters:
        parts = []
        for c in clusters[:6]:
            n_papers = len(c.get("papers") or [])
            parts.append(f"**{_short(c.get('label'), 24)}**（{n_papers} 篇）")
        lines.append("- 主题簇：" + "、".join(parts))
    years = sorted({int(n.get("year") or 0) for n in nodes if n.get("year")})
    if years:
        span = str(years[0]) if len(years) == 1 else f"{years[0]}–{years[-1]}"
        lines.append(f"- 时间跨度：{span}")
    n_found = sum(1 for n in nodes if n.get("role") == "foundational")
    if n_found:
        lines.append(f"- 奠基性论文 {n_found} 篇（谱系图见附件图片）")
    return "\n".join(lines)


def _card_reading_path(result: dict) -> str:
    path = [p for p in (result.get("path") or []) if isinstance(p, dict)]
    if not path:
        return ""
    lines = [f"**🧭 推荐阅读路径 · {len(path)} 篇**", ""]
    for i, p in enumerate(path[:_PATH_TOP_N], 1):
        role = _short(p.get("role"), 6) or "参考"
        reason = f" — {_short(p.get('reason'), 60)}" if p.get("reason") else ""
        lines.append(f"{i}. 【{role}】**{_short(p.get('title'), 70)}**{reason}")
    return "\n".join(lines)


_DEEP_FIELDS = (
    ("research_problem", "研究问题"),
    ("methodology", "方法"),
    ("key_findings", "主要发现"),
)


def _card_deep_read(result: dict) -> str:
    summaries = result.get("summaries") or {}
    if not isinstance(summaries, dict):
        summaries = {}
    full_ids = set(result.get("full_text_paper_ids") or [])
    fallback = result.get("abstract_fallback_papers") or []
    entries = [s for s in summaries.values() if isinstance(s, dict)]
    if not entries and not full_ids and not fallback:
        return ""
    head = (f"**📖 深度阅读 · {len(full_ids)} 篇全文级 / "
            f"{len(fallback)} 篇摘要级**")
    lines = [head]
    for s in entries[:_DEEP_READ_TOP_N]:
        lines.append("")
        lines.append(f"**{_short(s.get('title'), 70)}**")
        for field, label in _DEEP_FIELDS:
            if s.get(field):
                lines.append(f"- {label}：{_short(s.get(field), 100)}")
    if len(entries) > _DEEP_READ_TOP_N:
        lines.append(f"\n…等共 {len(entries)} 篇结构化摘要，可指定论文继续追问")
    return "\n".join(lines)


def _card_explain_element(result: dict) -> str:
    element = result.get("element")
    if not isinstance(element, dict) or not element.get("element_id"):
        return ""
    kind = str(element.get("kind") or "")
    label = _KIND_LABEL.get(kind, "元素")
    caption = _short(element.get("caption"), 70)
    head = f"**🔍 {label}解读{(' · ' + caption) if caption else ''}**"
    understanding = element.get("understanding") or {}
    lines = [head]
    if kind == "table":
        markdown = str((element.get("docling_extract") or {}).get("markdown") or "").strip()
        if markdown:
            lines += ["", f"```\n{markdown[:800]}\n```"]
    elif kind == "formula":
        latex = str((element.get("docling_extract") or {}).get("latex")
                    or understanding.get("latex") or "").strip()
        if latex:
            lines += ["", "```latex", latex[:400], "```"]
    if isinstance(understanding, dict) and understanding.get("description"):
        lines += ["", _short(understanding.get("description"), 260)]
    page = element.get("page")
    if page:
        lines.append(f"\n*位于第 {page} 页，原图见附件图片*")
    return "\n".join(lines)


def _card_field_census(result: dict) -> str:
    yearly = [y for y in (result.get("yearly") or []) if isinstance(y, dict)]
    if not yearly:
        return ""
    lines = ["**📊 领域普查**", ""]
    tail = yearly[-6:]
    lines.append("- 年度趋势：" + " → ".join(
        f"{_short(y.get('key') or y.get('name'), 6)}: {y.get('count', 0)}" for y in tail))
    for field, label in (("top_authors", "高产作者"),
                         ("top_institutions", "高产机构"),
                         ("top_venues", "主要期刊")):
        rows = [r for r in (result.get(field) or []) if isinstance(r, dict)][:5]
        if rows:
            lines.append(f"- {label}：" + "、".join(
                f"{_short(r.get('name'), 20)}（{r.get('count', 0)}）" for r in rows))
    lines.append("\n*趋势图见附件图片*")
    return "\n".join(lines)


def _card_write_review(result: dict) -> str:
    review = str(result.get("literature_review") or "").strip()
    if not review:
        return ""
    n_chars = result.get("review_chars") or len(review)
    return "\n".join([
        f"**📝 文献综述已生成 · {n_chars} 字**", "",
        _short(review, 300),
        "", "*全文见附件 Markdown 文件*",
    ])


def _card_citation_export(result: dict) -> str:
    citations = str(result.get("citations") or "").strip()
    if not citations:
        return ""
    fmt = "GB/T 7714" if result.get("format") == "gbt7714" else "BibTeX"
    count = result.get("count") or ""
    return "\n".join([
        f"**📑 参考文献导出 · {fmt} · {count} 篇**", "",
        "```",
        citations[:700],
        "```",
        "", "*完整文件见附件，可直接导入 LaTeX / Zotero*",
    ])


_FULL_CARDS = {
    "search_papers": _card_search_papers,
    "research_map": _card_research_map,
    "reading_path": _card_reading_path,
    "deep_read": _card_deep_read,
    "explain_element": _card_explain_element,
    "field_census": _card_field_census,
    "write_review": _card_write_review,
    "citation_export": _card_citation_export,
}


# ---------------------------------------------------------------------------
# One-line summary cards (every non-core tool)
# ---------------------------------------------------------------------------

def _one_liner(tool: str, result: dict) -> str:
    if result.get("status") == "error":
        message = (result.get("error") or {}).get("message") or result.get("summary") or ""
        return f"⚠️ {tool_label(tool)} · {_short(message, 90)}"
    summary = _short(result.get("summary") or result.get("text"), 110)
    mark = _status_mark(result)
    suffix = f" · {summary}" if summary else ""
    return f"{mark}{tool_label(tool)}{suffix}" if mark else f"**{tool_label(tool)}**{suffix}"


def render_tool_card(tool: str, result: dict, full_cards: frozenset[str] | set[str]) -> str | None:
    """Render one tool result as a markdown card; None when nothing to show.

    ``full_cards`` is the admin-configured set of tools that get full cards
    (core preset by default); every other known tool degrades to a one-liner.
    """
    try:
        renderer = _FULL_CARDS.get(tool)
        if renderer is not None and tool in full_cards and result.get("status") != "error":
            card = renderer(result)
            if card:
                return card[:MAX_CARD_CHARS]
            return _one_liner(tool, result)
        if tool in _TOOL_META:
            return _one_liner(tool, result)
        return None
    except Exception:  # noqa: BLE001 — a card must never break the turn
        try:
            return _one_liner(tool, result)
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Field census trend chart (portable static SVG, mirrors FieldCensusTrend)
# ---------------------------------------------------------------------------

_CENSUS_TREND_COLOR = "#256d66"
_CENSUS_BAR_COLORS = ("#4a628a", "#8a6d3b")


def render_field_census_svg(result: dict, title: str = "") -> str:
    """Yearly trend polyline + top authors/institutions bars, pure static SVG."""
    yearly = [y for y in (result.get("yearly") or []) if isinstance(y, dict)]
    if not yearly:
        return ""
    yearly = sorted(yearly, key=lambda y: int(y.get("key") or 0))

    width = 880
    margin_l, margin_r = 76, 30
    trend_top, trend_h = 66, 200
    bars_top = trend_top + trend_h + 46
    row_h, row_gap = 20, 12
    top_rows = 5
    bars_h = top_rows * (row_h + row_gap) + 34
    height = bars_top + bars_h + 16

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfaf7"/>',
        '<style>text{font-family:Arial,"Noto Sans CJK SC","PingFang SC",'
        '"Microsoft YaHei",sans-serif}.title{fill:#17212b;font-size:19px;font-weight:700}'
        '.sub{fill:#6b7280;font-size:12px}.axis{fill:#6b7280;font-size:11px}'
        '.label{fill:#24303f;font-size:12px}.val{fill:#4b5563;font-size:11px}</style>',
        f'<text x="{margin_l}" y="34" class="title">{html.escape(title or "领域普查")}</text>',
        f'<text x="{margin_l}" y="52" class="sub">年度发文趋势与高产作者 / 机构（OpenAlex 聚合）</text>',
    ]

    # --- trend polyline ---
    counts = [max(0, int(y.get("count") or 0)) for y in yearly]
    max_count = max(counts) or 1
    plot_w = width - margin_l - margin_r
    step = plot_w / max(1, len(yearly) - 1)
    ys = trend_top + trend_h
    points = []
    for i, c in enumerate(counts):
        x = margin_l + i * step
        y = ys - (c / max_count) * (trend_h - 24)
        points.append((x, y))
    # gridlines + y labels
    for frac, tag in ((0.0, ""), (0.5, ""), (1.0, "")):
        gy = ys - frac * (trend_h - 24)
        parts.append(f'<line x1="{margin_l}" y1="{gy:.1f}" x2="{width - margin_r}" '
                     f'y2="{gy:.1f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{margin_l - 8}" y="{gy + 4:.1f}" text-anchor="end" '
                     f'class="axis">{int(max_count * frac)}</text>')
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    fill_poly = f"{margin_l},{ys} " + poly + f" {width - margin_r},{ys}"
    parts.append(f'<polygon points="{fill_poly}" fill="{_CENSUS_TREND_COLOR}" opacity="0.08"/>')
    parts.append(f'<polyline points="{poly}" fill="none" stroke="{_CENSUS_TREND_COLOR}" '
                 f'stroke-width="2.5" stroke-linejoin="round"/>')
    label_every = max(1, (len(yearly) + 7) // 8)
    for i, (x, y) in enumerate(points):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{_CENSUS_TREND_COLOR}"><title>'
                     f'{html.escape(str(yearly[i].get("key") or ""))}: {counts[i]}</title></circle>')
        if i % label_every == 0 or i == len(points) - 1:
            parts.append(f'<text x="{x:.1f}" y="{ys + 18}" text-anchor="middle" class="axis">'
                         f'{html.escape(str(yearly[i].get("key") or yearly[i].get("name") or ""))}</text>')

    # --- top-N horizontal bars (authors left, institutions right) ---
    def bar_group(x0: int, field: str, header: str, color: str) -> None:
        rows = [r for r in (result.get(field) or []) if isinstance(r, dict)][:top_rows]
        parts.append(f'<text x="{x0}" y="{bars_top}" class="label" '
                     f'font-weight="700">{html.escape(header)}</text>')
        if not rows:
            parts.append(f'<text x="{x0}" y="{bars_top + 24}" class="axis">（无数据）</text>')
            return
        row_max = max(int(r.get("count") or 0) for r in rows) or 1
        name_w, bar_max_w = 204, 110  # 16 CJK chars @12px ≈ 192px, then the bar
        for j, r in enumerate(rows):
            y = bars_top + 18 + j * (row_h + row_gap)
            w = max(2, (int(r.get("count") or 0) / row_max) * bar_max_w)
            parts.append(f'<text x="{x0}" y="{y + 14}" class="label">'
                         f'{html.escape(_short(r.get("name"), 16))}</text>')
            parts.append(f'<rect x="{x0 + name_w}" y="{y}" width="{w:.1f}" height="{row_h}" '
                         f'rx="3" fill="{color}" opacity="0.85"/>')
            parts.append(f'<text x="{x0 + name_w + w + 8:.1f}" y="{y + 14}" class="val">'
                         f'{int(r.get("count") or 0)}</text>')

    half = (width - margin_l - margin_r) // 2
    bar_group(margin_l, "top_authors", "高产作者 Top 5", _CENSUS_BAR_COLORS[0])
    bar_group(margin_l + half + 10, "top_institutions", "高产机构 Top 5", _CENSUS_BAR_COLORS[1])

    parts.append("</svg>")
    return "".join(parts)
