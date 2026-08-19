"""Markdown report rendering + file writing for x_soda.attachments output.

When a /v1 turn produced research artifacts (research map, reading path,
literature review), they are rendered into markdown files under
data/exports/ and served via GET /files/{name}; the file URLs go into the
response's x_soda.attachments so 清小搭 can hand them to the user.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from agents.session import ChatSession

_EXPORT_DIR = Path(__file__).resolve().parents[2] / "data" / "exports"


def _paper_line(p) -> str:
    year = p.year or "n.d."
    cites = p.citation_count or 0
    url = ""
    if p.urls:
        url = next(iter(p.urls.values()), "")
    if not url and p.doi:
        url = f"https://doi.org/{p.doi.lstrip('/')}"
    link = f" ([链接]({url}))" if url else ""
    return f"- **{p.title}** ({year}, 被引 {cites}){link}"


def render_research_report(session: ChatSession) -> str:
    """研究地图 + 阅读路径 + 论文清单 as one markdown document."""
    parts = [f"# 研究地图：{session.topic or '未命名主题'}\n"]
    md = session.map_data or {}

    landscape = md.get("landscape", "")
    if landscape:
        parts.append(f"## 领域脉络\n\n{landscape}\n")

    clusters = md.get("clusters", [])
    if clusters:
        parts.append("## 主题簇\n")
        for c in clusters:
            parts.append(f"### {c.get('label', '未命名簇')}")
            if c.get("overview"):
                parts.append(c["overview"])
            for p in c.get("papers", []):
                year = p.get("year") or "n.d."
                parts.append(f"- {p.get('title', '')} ({year}, 被引 {p.get('citation_count', 0)})")
            parts.append("")

    timeline = md.get("timeline", [])
    if timeline:
        parts.append("## 时间脉络\n")
        for t in timeline:
            titles = "；".join(p["title"] for p in t.get("papers", [])[:3])
            parts.append(f"- **{t['year']}**：{titles}")

    if session.reading_path:
        parts.append("\n## 推荐阅读路径\n")
        for i, p in enumerate(session.reading_path, 1):
            reason = f" — {p['reason']}" if p.get("reason") else ""
            parts.append(f"{i}. [{p['role']}] **{p['title']}**{reason}")

    if session.papers:
        parts.append("\n## 核心集论文\n")
        parts.extend(_paper_line(p) for p in session.papers)
    return "\n".join(parts).strip() + "\n"



_CLUSTER_COLORS = ["#256d66", "#c2402a", "#4a628a", "#8a6d3b", "#6b4a8a",
                   "#3b7a4a", "#a05a2c", "#4a8a82"]

# Mirrors frontend genealogy-layout.ts nodeRadius(): three fixed citation
# tiers — visually calm, no continuous scale.
_NODE_TIERS = ((500, 13), (50, 10), (0, 7))

_AGGREGATE_THRESHOLD = 3  # >3 papers in one (cluster, year) bucket collapse to "+N"


def _node_radius(citation_count: int) -> int:
    for threshold, radius in _NODE_TIERS:
        if citation_count >= threshold:
            return radius
    return 7


def render_research_map_svg(session: ChatSession) -> str:
    """Render the research-map graph as a portable, static SVG attachment.

    The self-hosted frontend keeps the fully interactive React graph. OpenAI-
    compatible hosts (including 清小搭) cannot execute that component, so this
    deterministic SVG provides the same nodes/edges as an ordinary image card,
    visually aligned with the frontend: cluster lanes with colored labels,
    citation-tier node sizes, foundational halos, "+N" aggregate buckets,
    year gridlines and a legend.
    """
    import html
    import time as _time

    def _short(text, limit):
        s = str(text or "").strip()
        return s[:limit] + ("…" if len(s) > limit else "")

    md = session.map_data or {}
    graph = md.get("graph") or {}
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    if not nodes:
        return ""
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    years = sorted({int(n.get("year") or 0) for n in nodes})
    cluster_ids = sorted({int(n.get("cluster") or 0) for n in nodes})
    cluster_labels = {
        int(c.get("id") or 0): str(c.get("label") or "")
        for c in (md.get("clusters") or []) if isinstance(c, dict)
    }
    year_pos = {year: i for i, year in enumerate(years)}
    cluster_pos = {c: i for i, c in enumerate(cluster_ids)}

    left, right, top = 150, 46, 96
    legend_h = 46
    x_step = 118
    y_step = 112
    width = max(980, left + right + max(1, len(years) - 1) * x_step)
    lanes_h = max(1, len(cluster_ids)) * y_step
    height = top + lanes_h + legend_h

    # Bucket nodes by (cluster, year); dense buckets collapse to one "+N" node.
    buckets: dict[tuple[int, int], list[dict]] = {}
    for n in nodes:
        buckets.setdefault((int(n.get("cluster") or 0), int(n.get("year") or 0)), []).append(n)
    positions: dict[str, tuple[float, float]] = {}
    aggregate_nodes: list[tuple[float, float, int, int]] = []  # x, y, cluster, count
    for (cluster, year), members in buckets.items():
        cx = left + year_pos[year] * x_step
        lane_top = top + cluster_pos[cluster] * y_step
        lane_mid = lane_top + y_step * 0.5
        if len(members) > _AGGREGATE_THRESHOLD:
            aggregate_nodes.append((cx, lane_mid, cluster, len(members)))
            continue
        k = len(members)
        for i, n in enumerate(members):
            dy = (i - (k - 1) / 2) * min(30, y_step / (k + 1))
            positions[str(n["id"])] = (cx, lane_mid + dy)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfaf7"/>',
        '<style>text{font-family:Arial,"Noto Sans CJK SC","PingFang SC",'
        '"Microsoft YaHei",sans-serif}.title{fill:#17212b;font-size:21px;font-weight:700}'
        '.sub{fill:#6b7280;font-size:12.5px}.axis{fill:#6b7280;font-size:12px}'
        '.lane{fill:#4b5563;font-size:12px}.label{fill:#24303f;font-size:10.5px}'
        '.agg{fill:#ffffff;font-size:11px;font-weight:700}.legend{fill:#6b7280;font-size:11.5px}'
        '.edge{stroke:#94a3b8;stroke-width:1.2;opacity:.5}'
        '.semantic{stroke-dasharray:5 4;opacity:.32}</style>',
        f'<text x="{left}" y="34" class="title">{html.escape(_short(session.topic or "研究谱系图", 60))}</text>',
        f'<text x="{left}" y="56" class="sub">研究谱系 · {len(nodes)} 篇论文 · '
        f'{len(cluster_ids)} 个主题簇 · {len(edges)} 条关联 · '
        f'{_time.strftime("%Y-%m-%d")} 生成</text>',
    ]

    # Year gridlines + labels (thin out labels when the span is wide).
    label_every = max(1, (len(years) + 9) // 10)
    for year, idx in year_pos.items():
        x = left + idx * x_step
        parts.append(f'<line x1="{x:.1f}" y1="66" x2="{x:.1f}" y2="{top + lanes_h}" '
                     f'stroke="#e5e7eb"/>')
        if idx % label_every == 0 or idx == len(years) - 1:
            parts.append(f'<text x="{x:.1f}" y="82" text-anchor="middle" class="axis">'
                         f'{year or "n.d."}</text>')

    # Cluster lanes: alternating band + colored label on the left.
    for cluster, idx in cluster_pos.items():
        lane_top = top + idx * y_step
        if idx % 2 == 0:
            parts.append(f'<rect x="{left - 10}" y="{lane_top}" width="{width - left - right + 20}" '
                         f'height="{y_step}" fill="#f2f0ea" opacity="0.55"/>')
        color = _CLUSTER_COLORS[cluster % len(_CLUSTER_COLORS)]
        label = cluster_labels.get(cluster) or f"簇 {cluster}"
        parts.append(f'<circle cx="{left - 96}" cy="{lane_top + y_step * 0.5}" r="5" fill="{color}"/>')
        parts.append(f'<text x="{left - 84}" y="{lane_top + y_step * 0.5 + 4}" class="lane">'
                     f'{html.escape(_short(label, 12))}</text>')

    # Edges first so nodes sit on top; edges into collapsed buckets are dropped.
    for edge in edges:
        source = positions.get(str(edge.get("source")))
        target = positions.get(str(edge.get("target")))
        if not source or not target:
            continue
        klass = "edge semantic" if edge.get("type") == "semantic" else "edge"
        parts.append(f'<line x1="{source[0]:.1f}" y1="{source[1]:.1f}" '
                     f'x2="{target[0]:.1f}" y2="{target[1]:.1f}" class="{klass}"/>')

    for node in nodes:
        pos = positions.get(str(node["id"]))
        if pos is None:
            continue
        x, y = pos
        cluster = int(node.get("cluster") or 0)
        color = _CLUSTER_COLORS[cluster % len(_CLUSTER_COLORS)]
        title = str(node.get("title") or node["id"])
        cites = int(node.get("citation_count") or 0)
        radius = _node_radius(cites)
        hover = html.escape(f"{title}（{node.get('year') or 'n.d.'} · 被引 {cites}）")
        if node.get("role") == "foundational":
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius + 4}" fill="{color}" '
                         f'opacity="0.22"/>')
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{color}" '
                     f'stroke="#ffffff" stroke-width="1.4"><title>{hover}</title></circle>')
        if node.get("layer") != "core":
            parts.append(f'<text x="{x:.1f}" y="{y - radius - 5:.1f}" text-anchor="middle" '
                         f'class="label" opacity="0.75">{html.escape(_short(title, 18))}</text>')
        else:
            parts.append(f'<text x="{x:.1f}" y="{y + radius + 13:.1f}" text-anchor="middle" '
                         f'class="label">{html.escape(_short(title, 18))}</text>')

    for x, y, cluster, count in aggregate_nodes:
        color = _CLUSTER_COLORS[cluster % len(_CLUSTER_COLORS)]
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="13" fill="{color}" '
                     f'stroke="#ffffff" stroke-width="1.6"/>')
        parts.append(f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" class="agg">'
                     f'+{count}</text>')

    # Legend.
    ly = top + lanes_h + 30
    grey = "#9ca3af"
    legend: list[tuple[str, int, str]] = [
        (f'<circle cx="{{x}}" cy="{ly - 4}" r="13" fill="{grey}"/>', 10, "被引 ≥ 500"),
        (f'<circle cx="{{x}}" cy="{ly - 4}" r="10" fill="{grey}"/>', 104, "50–499"),
        (f'<circle cx="{{x}}" cy="{ly - 4}" r="7" fill="{grey}"/>', 196, "< 50"),
        ('', 266, "光环 = 奠基性论文"),
        (f'<line x1="{{x}}" y1="{ly - 4}" x2="{{x2}}" y2="{ly - 4}" '
         f'stroke="#94a3b8" stroke-width="1.6"/>', 412, "引用关系"),
        (f'<line x1="{{x}}" y1="{ly - 4}" x2="{{x2}}" y2="{ly - 4}" '
         f'stroke="#94a3b8" stroke-width="1.6" stroke-dasharray="5 4"/>', 522, "语义相似"),
    ]
    for glyph, offset, text in legend:
        x = left + offset
        parts.append(glyph.replace("{x}", f"{x:.1f}").replace("{x2}", f"{x + 32:.1f}"))
        parts.append(f'<text x="{x + 22:.1f}" y="{ly}" class="legend">'
                     f'{html.escape(text)}</text>')
    parts.append(f'<text x="{width - right}" y="{ly}" text-anchor="end" class="legend">'
                 f'标签在上方 = 候选层 · 下方 = 核心层</text>')
    parts.append('</svg>')
    return "".join(parts)

def _graph_payload(session: ChatSession) -> tuple[dict, list[dict], list[dict]]:
    md = session.map_data or {}
    graph = md.get("graph") or {}
    nodes = [
        n for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and str(n.get("id") or "").strip()
    ]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    return md, nodes, edges


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _short_text(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _fit_display_text(value, max_units: int) -> str:
    """Truncate mixed Latin/CJK text by approximate rendered width."""
    import unicodedata

    text = " ".join(str(value or "").split())
    used = 0
    kept: list[str] = []
    for char in text:
        units = 2 if unicodedata.east_asian_width(char) in {"W", "F", "A"} else 1
        if used + units > max_units:
            return "".join(kept) + "…"
        kept.append(char)
        used += units
    return "".join(kept)


def render_pretty_research_map_svg(session: ChatSession) -> str:
    """Render a deterministic, self-contained SVG optimized for file cards.

    The document uses only SVG primitives and an internal stylesheet: no
    JavaScript, external fonts, external CSS, remote images, or filters. Dense
    (theme, year) buckets collapse deterministically and excess theme clusters
    share a final fallback lane, keeping a stable viewBox on narrow hosts.
    """
    import html
    import time as _time

    md, raw_nodes, raw_edges = _graph_payload(session)
    if not raw_nodes:
        return ""

    nodes = sorted(
        raw_nodes,
        key=lambda n: (
            _safe_int(n.get("cluster")), _safe_int(n.get("year")),
            str(n.get("title") or "").casefold(), str(n.get("id")),
        ),
    )
    node_ids = {str(n["id"]) for n in nodes}
    edges = [
        e for e in raw_edges
        if str(e.get("source") or "") in node_ids
        and str(e.get("target") or "") in node_ids
    ]
    edges.sort(key=lambda e: (
        str(e.get("type") or ""), str(e.get("source") or ""),
        str(e.get("target") or ""),
    ))

    cluster_labels = {
        _safe_int(c.get("id")): str(c.get("label") or "")
        for c in (md.get("clusters") or []) if isinstance(c, dict)
    }
    original_clusters = sorted({_safe_int(n.get("cluster")) for n in nodes})
    max_lanes = 10
    visible_clusters = original_clusters[:max_lanes]
    overflow_clusters = set(original_clusters[max_lanes - 1:]) if len(original_clusters) > max_lanes else set()
    if overflow_clusters:
        visible_clusters = original_clusters[:max_lanes - 1] + [-1]

    def lane_for(cluster: int) -> int:
        return -1 if cluster in overflow_clusters else cluster

    lane_index = {cluster: index for index, cluster in enumerate(visible_clusters)}
    year_values = sorted({_safe_int(n.get("year")) for n in nodes})
    max_year_columns = 9
    group_size = max(1, (len(year_values) + max_year_columns - 1) // max_year_columns)
    year_groups = [
        year_values[index:index + group_size]
        for index in range(0, len(year_values), group_size)
    ]
    year_slot = {
        year: slot for slot, group in enumerate(year_groups) for year in group
    }

    def year_group_label(group: list[int]) -> str:
        labels = [str(year) if year else "n.d." for year in group]
        return labels[0] if len(labels) == 1 else f"{labels[0]}–{labels[-1]}"

    year_labels = [year_group_label(group) for group in year_groups]

    width = 1240
    left, right, top = 194, 44, 122
    bottom = 92
    lane_height = 136
    card_width, card_height = 152, 42
    plot_width = width - left - right
    timeline_left = left + card_width / 2
    timeline_width = width - right - card_width / 2 - timeline_left
    plot_height = max(1, len(visible_clusters)) * lane_height
    height = top + plot_height + bottom
    x_step = timeline_width / max(1, len(year_labels) - 1)

    buckets: dict[tuple[int, int], list[dict]] = {}
    for node in nodes:
        key = (
            lane_for(_safe_int(node.get("cluster"))),
            year_slot[_safe_int(node.get("year"))],
        )
        buckets.setdefault(key, []).append(node)

    positions: dict[str, tuple[float, float]] = {}
    aliases: dict[str, str] = {}
    aggregates: list[dict] = []
    shown_nodes: list[dict] = []
    for (cluster, slot), members in sorted(buckets.items()):
        x = timeline_left + slot * x_step
        lane_y = top + lane_index[cluster] * lane_height + lane_height / 2
        if len(members) > 2:
            aggregate_id = f"aggregate:{cluster}:{slot}"
            aggregate = {
                "id": aggregate_id, "cluster": cluster, "year_label": year_labels[slot],
                "members": members, "x": x, "y": lane_y,
            }
            aggregates.append(aggregate)
            positions[aggregate_id] = (x, lane_y)
            for member in members:
                aliases[str(member["id"])] = aggregate_id
            continue
        offset = 30 if len(members) == 2 else 0
        for index, node in enumerate(members):
            y = lane_y + (index * 2 - 1) * offset if len(members) == 2 else lane_y
            node_id = str(node["id"])
            positions[node_id] = (x, y)
            aliases[node_id] = node_id
            shown_nodes.append(node)

    def endpoint(node_id: str) -> str:
        return aliases.get(node_id, node_id)

    drawn_edges: list[tuple[str, str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for edge in edges:
        source = endpoint(str(edge.get("source") or ""))
        target = endpoint(str(edge.get("target") or ""))
        edge_type = "semantic" if str(edge.get("type") or "") == "semantic" else "cites"
        key = (source, target, edge_type)
        if source == target or source not in positions or target not in positions or key in seen_edges:
            continue
        seen_edges.add(key)
        drawn_edges.append(key)

    esc = lambda value: html.escape(str(value or ""), quote=True)
    title = esc(_short_text(session.topic or "研究谱系图", 70))
    citation_count = sum(1 for e in edges if str(e.get("type") or "") != "semantic")
    semantic_count = len(edges) - citation_count
    generated = _time.strftime("%Y-%m-%d")
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="auto" '
        f'viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" '
        f'role="img" aria-labelledby="map-title map-desc">',
        '<title id="map-title">研究引用关系图谱</title>',
        f'<desc id="map-desc">{len(nodes)} 篇论文、{len(original_clusters)} 个主题簇、'
        f'{citation_count} 条直接引用、{semantic_count} 条语义关联。</desc>',
        '<defs><marker id="arrow-cite" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#52677d"/></marker>'
        '<marker id="arrow-semantic" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 1 L 9 5 L 0 9" fill="none" stroke="#9a6e52" stroke-width="1.5"/>'
        '</marker></defs>',
        '<style>text{font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}'
        '.heading{font-size:24px;font-weight:700;fill:#18302f}.meta{font-size:12px;fill:#65736f}'
        '.year{font-size:11px;fill:#75827e}.lane-label{font-size:12px;font-weight:650;fill:#334945}'
        '.node-title{font-size:10.5px;font-weight:650;fill:#203733}.node-meta{font-size:9.5px;fill:#6a7874}'
        '.agg-title{font-size:13px;font-weight:750;fill:#fff}.agg-meta{font-size:9.5px;fill:#e7f3f0}'
        '.legend{font-size:10.5px;fill:#60706b}.cite-edge{fill:none;stroke:#52677d;stroke-width:1.45;opacity:.66}'
        '.semantic-edge{fill:none;stroke:#9a6e52;stroke-width:1.25;stroke-dasharray:6 5;opacity:.58}</style>',
        '<rect width="100%" height="100%" rx="18" fill="#fbfcfa"/>',
        f'<text x="{left}" y="38" class="heading">{title}</text>',
        f'<text x="{left}" y="62" class="meta">引用关系图谱 · {len(nodes)} 篇论文 · '
        f'{len(original_clusters)} 个主题簇 · {citation_count} 条引用边 · {semantic_count} 条语义边 · '
        f'{generated} 生成</text>',
        '<text x="194" y="88" class="meta">箭头表示关系方向；虚线表示语义关联；同年同主题的密集论文会折叠为聚合节点。</text>',
    ]

    for index, year_label in enumerate(year_labels):
        x = timeline_left + index * x_step
        parts.append(
            f'<line x1="{x:.1f}" y1="{top - 18}" x2="{x:.1f}" y2="{top + plot_height}" '
            'stroke="#dfe8e4" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{top - 26}" text-anchor="middle" class="year">'
            f'{esc(year_label)}</text>'
        )

    for cluster, index in lane_index.items():
        lane_top = top + index * lane_height
        color = "#64748b" if cluster == -1 else _CLUSTER_COLORS[cluster % len(_CLUSTER_COLORS)]
        label = "其他主题" if cluster == -1 else (cluster_labels.get(cluster) or f"主题簇 {cluster}")
        parts.append(
            f'<rect x="{left - 10}" y="{lane_top}" width="{plot_width + 20:.1f}" '
            f'height="{lane_height}" rx="12" fill="{color}" opacity="{0.035 if index % 2 else 0.065}"/>'
        )
        parts.append(
            f'<rect x="28" y="{lane_top + 43:.1f}" width="140" height="38" rx="12" '
            f'fill="{color}" opacity=".12"/>'
            f'<rect x="28" y="{lane_top + 43:.1f}" width="5" height="38" rx="2.5" fill="{color}"/>'
            f'<text x="43" y="{lane_top + 67:.1f}" class="lane-label">{esc(_short_text(label, 17))}</text>'
        )

    for source, target, edge_type in drawn_edges:
        sx, sy = positions[source]
        tx, ty = positions[target]
        direction = 1 if tx >= sx else -1
        start_x = sx + direction * (card_width / 2 if not source.startswith("aggregate:") else 31)
        end_x = tx - direction * (card_width / 2 if not target.startswith("aggregate:") else 31)
        bend = max(24, abs(end_x - start_x) * .38)
        c1 = start_x + direction * bend
        c2 = end_x - direction * bend
        css = "semantic-edge" if edge_type == "semantic" else "cite-edge"
        marker = "arrow-semantic" if edge_type == "semantic" else "arrow-cite"
        parts.append(
            f'<path d="M {start_x:.1f} {sy:.1f} C {c1:.1f} {sy:.1f}, {c2:.1f} {ty:.1f}, '
            f'{end_x:.1f} {ty:.1f}" class="{css}" marker-end="url(#{marker})"/>'
        )

    for node in shown_nodes:
        node_id = str(node["id"])
        x, y = positions[node_id]
        cluster = lane_for(_safe_int(node.get("cluster")))
        color = "#64748b" if cluster == -1 else _CLUSTER_COLORS[cluster % len(_CLUSTER_COLORS)]
        foundational = str(node.get("role") or "") == "foundational"
        layer = str(node.get("layer") or "core")
        if foundational:
            parts.append(
                f'<rect x="{x - card_width / 2 - 4:.1f}" y="{y - card_height / 2 - 4:.1f}" '
                f'width="{card_width + 8}" height="{card_height + 8}" rx="13" fill="none" '
                f'stroke="{color}" stroke-width="2" stroke-dasharray="3 2" opacity=".55"/>'
            )
        parts.append(
            f'<rect x="{x - card_width / 2:.1f}" y="{y - card_height / 2:.1f}" '
            f'width="{card_width}" height="{card_height}" rx="10" fill="#ffffff" '
            f'stroke="{color}" stroke-width="{1.8 if foundational else 1.2}"/>'
            f'<rect x="{x - card_width / 2:.1f}" y="{y - card_height / 2:.1f}" '
            f'width="6" height="{card_height}" rx="3" fill="{color}"/>'
        )
        if layer == "candidate":
            parts.append(
                f'<circle cx="{x + card_width / 2 - 10:.1f}" cy="{y - card_height / 2 + 10:.1f}" '
                'r="4" fill="none" stroke="#8a9894" stroke-width="1.2"/>'
            )
        parts.append(
            f'<text x="{x - card_width / 2 + 14:.1f}" y="{y - 3:.1f}" class="node-title">'
            f'{esc(_fit_display_text(node.get("title"), 22))}</text>'
            f'<text x="{x - card_width / 2 + 14:.1f}" y="{y + 13:.1f}" class="node-meta">'
            f'{esc(node.get("year") or "n.d.")} · 被引 {_safe_int(node.get("citation_count"))}'
            f'{" · 奠基" if foundational else ""}</text>'
        )

    for aggregate in aggregates:
        x, y = aggregate["x"], aggregate["y"]
        cluster = aggregate["cluster"]
        color = "#64748b" if cluster == -1 else _CLUSTER_COLORS[cluster % len(_CLUSTER_COLORS)]
        count = len(aggregate["members"])
        parts.append(
            f'<rect x="{x - 31:.1f}" y="{y - 24:.1f}" width="62" height="48" rx="15" '
            f'fill="{color}" stroke="#fff" stroke-width="3"/>'
            f'<text x="{x:.1f}" y="{y - 1:.1f}" text-anchor="middle" class="agg-title">+{count}</text>'
            f'<text x="{x:.1f}" y="{y + 14:.1f}" text-anchor="middle" class="agg-meta">聚合论文</text>'
        )

    legend_y = top + plot_height + 46
    legend = [
        ('<line x1="0" y1="0" x2="30" y2="0" class="cite-edge" marker-end="url(#arrow-cite)"/>', "直接引用"),
        ('<line x1="0" y1="0" x2="30" y2="0" class="semantic-edge" marker-end="url(#arrow-semantic)"/>', "语义关联"),
        ('<rect x="0" y="-11" width="22" height="18" rx="5" fill="#fff" stroke="#256d66"/>', "主题簇论文"),
        ('<rect x="0" y="-13" width="26" height="22" rx="7" fill="none" stroke="#256d66" stroke-dasharray="3 2"/>', "奠基性论文"),
        ('<rect x="0" y="-13" width="30" height="24" rx="8" fill="#64748b"/>', "聚合节点"),
    ]
    x = 194.0
    for glyph, label in legend:
        parts.append(f'<g transform="translate({x:.1f} {legend_y:.1f})">{glyph}</g>')
        parts.append(f'<text x="{x + 38:.1f}" y="{legend_y + 4:.1f}" class="legend">{label}</text>')
        x += 178
    parts.append('</svg>')
    return "".join(parts)


def _markdown_cell(value) -> str:
    return " ".join(str(value or "").split()).replace("|", "\\|").replace("\n", " ")


def render_pretty_research_map_markdown(session: ChatSession) -> str:
    """Portable relation listing paired with the pretty SVG; never Mermaid."""
    from urllib.parse import urlsplit

    md, nodes, edges = _graph_payload(session)
    node_by_id = {str(n["id"]): n for n in nodes}
    paper_by_id = {str(p.id): p for p in session.all_papers() if getattr(p, "id", None)}
    cluster_labels = {
        _safe_int(c.get("id")): str(c.get("label") or "")
        for c in (md.get("clusters") or []) if isinstance(c, dict)
    }

    def paper_link(node: dict) -> str:
        paper = paper_by_id.get(str(node.get("id") or ""))
        doi = str(node.get("doi") or getattr(paper, "doi", "") or "").strip()
        url = str(node.get("url") or "").strip()
        if not url and paper is not None and paper.urls:
            url = next(iter(paper.urls.values()), "")
        if not url and doi:
            url = f"https://doi.org/{doi.removeprefix('https://doi.org/').removeprefix('doi:')}"
        parsed = urlsplit(url)
        safe_url = url if parsed.scheme in {"http", "https"} and parsed.netloc else ""
        label = f"DOI: {doi}" if doi else "来源"
        return f"[{_markdown_cell(label)}]({safe_url})" if safe_url else _markdown_cell(doi or "—")

    parts = [
        f"# 引用关系说明：{_markdown_cell(session.topic or '未命名主题')}",
        "",
        "> 本文件是静态 SVG 图谱的可复制说明，不依赖 Mermaid、JavaScript 或 HTML 绘图。",
        "",
        "## 图例",
        "",
        "- **直接引用**：实线箭头。",
        "- **语义关联**：虚线箭头。",
        "- **主题簇**：节点左侧色条与图谱泳道。",
        "- **奠基性论文**：虚线外框。",
        "- **聚合节点**：同一主题簇、同一年超过 2 篇时折叠；下方列出全部成员。",
        "",
    ]
    clusters = [c for c in (md.get("clusters") or []) if isinstance(c, dict)]
    if clusters:
        parts.extend(["## 主题簇摘要", ""])
        for cluster in sorted(clusters, key=lambda c: _safe_int(c.get("id"))):
            label = _markdown_cell(cluster.get("label") or f"主题簇 {_safe_int(cluster.get('id'))}")
            overview = " ".join(str(cluster.get("overview") or "").split())
            parts.append(f"- **{label}**：{overview or '暂无摘要'}")
        parts.append("")

    parts.extend([
        "## 论文清单",
        "",
        "| 年份 | 主题簇 | 论文 | 角色 | 被引 | DOI / 来源 |",
        "|---:|---|---|---|---:|---|",
    ])
    for node in sorted(nodes, key=lambda n: (
        _safe_int(n.get("year")), _safe_int(n.get("cluster")),
        str(n.get("title") or "").casefold(),
    )):
        year = node.get("year") or "n.d."
        cluster = _safe_int(node.get("cluster"))
        parts.append(
            f"| {_markdown_cell(year)} | {_markdown_cell(cluster_labels.get(cluster) or f'主题簇 {cluster}')} "
            f"| {_markdown_cell(node.get('title') or node.get('id'))} "
            f"| {_markdown_cell(node.get('role') or '—')} "
            f"| {_safe_int(node.get('citation_count'))} | {paper_link(node)} |"
        )

    parts.extend(["", "## 引用与语义关系", ""])
    if not edges:
        parts.append("暂无关系边。")
    else:
        parts.extend([
            "| 类型 | 起点 | 终点 |",
            "|---|---|---|",
        ])
        for edge in sorted(edges, key=lambda e: (
            str(e.get("type") or ""), str(e.get("source") or ""), str(e.get("target") or ""),
        )):
            source = node_by_id.get(str(edge.get("source") or ""), {})
            target = node_by_id.get(str(edge.get("target") or ""), {})
            relation = "语义关联" if str(edge.get("type") or "") == "semantic" else "直接引用"
            parts.append(
                f"| {relation} | {_markdown_cell(source.get('title') or edge.get('source'))} "
                f"| {_markdown_cell(target.get('title') or edge.get('target'))} |"
            )

    dense = {}
    for node in nodes:
        dense.setdefault((_safe_int(node.get("cluster")), _safe_int(node.get("year"))), []).append(node)
    aggregated = [(key, members) for key, members in sorted(dense.items()) if len(members) > 2]
    if aggregated:
        parts.extend(["", "## 聚合节点展开", ""])
        for (cluster, year), members in aggregated:
            label = cluster_labels.get(cluster) or f"主题簇 {cluster}"
            parts.append(f"### {_markdown_cell(label)} · {year or 'n.d.'}（{len(members)} 篇）")
            parts.extend(f"- {_markdown_cell(n.get('title') or n.get('id'))}" for n in members)
            parts.append("")

    parts.extend([
        "## 使用提示",
        "",
        "- SVG 是主图形附件，可无损缩放或下载后在浏览器中打开。",
        "- 若当前客户端不预览 SVG，可直接使用本文件中的论文表格和关系清单。",
        "- 自有 Web 前端仍提供可交互的筛选、缩放和论文详情。",
        "",
    ])
    return "\n".join(parts)

def render_review_report(session: ChatSession) -> str:
    """文献综述 as a standalone markdown document."""
    title = session.topic or "未命名主题"
    return f"# 文献综述：{title}\n\n{session.literature_review}\n"


def write_reports(
    session: ChatSession, kinds: set[str], *,
    research_map_render_strategy: str = "legacy_svg",
) -> list[dict]:
    """Render and persist artifacts under the selected server-side strategy.

    ``legacy_svg`` intentionally preserves the historical Markdown + SVG pair.
    ``pretty_svg`` emits only the optimized vector graph; the third strategy
    pairs that graph with a portable, Mermaid-free relation listing.
    """
    api_context = session.storage_context if session.channel == "openai_api" else None
    if api_context is None:
        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    else:
        api_context.ensure_layout()
    out: list[dict] = []
    renders: list[tuple[str, str, str, str, str]] = []  # kind, filename, text, type, mime
    stamp = time.strftime("%Y%m%d_%H%M%S")

    if "research_map" in kinds and session.map_data:
        if research_map_render_strategy == "legacy_svg":
            renders.append((
                "map", f"research_map_{stamp}_{uuid.uuid4().hex[:6]}.md",
                render_research_report(session), "text", "text/markdown",
            ))
            svg = render_research_map_svg(session)
        elif research_map_render_strategy in {"pretty_svg", "pretty_svg_markdown"}:
            svg = render_pretty_research_map_svg(session)
            if research_map_render_strategy == "pretty_svg_markdown":
                renders.append((
                    "map", f"research_map_relations_{stamp}_{uuid.uuid4().hex[:6]}.md",
                    render_pretty_research_map_markdown(session), "text", "text/markdown",
                ))
        else:
            # Defense in depth for non-admin/library callers. The policy store
            # and API schema reject unknown values before this point.
            svg = render_research_map_svg(session)
        if svg:
            renders.append((
                "map_svg", f"research_graph_{stamp}_{uuid.uuid4().hex[:6]}.svg",
                svg, "image", "image/svg+xml",
            ))
    if "write_review" in kinds and session.literature_review:
        renders.append(("review", f"literature_review_{stamp}_{uuid.uuid4().hex[:6]}.md",
                        render_review_report(session), "text", "text/markdown"))

    for _kind, filename, text, file_type, mime_type in renders:
        try:
            if api_context is not None:
                from core.api_artifact_store import ApiArtifactStore
                from core.api_storage_store import ApiStorageStore
                temp = api_context.temp_dir / f"export-{uuid.uuid4().hex}.tmp"
                temp.write_text(text, encoding="utf-8")
                artifact = ApiArtifactStore(ApiStorageStore(api_context)).save_export(
                    temp, session_id=session.session_id, display_name=filename,
                    mime_type=mime_type,
                )
                temp.unlink(missing_ok=True)
                out.append({
                    "fileName": artifact.public_alias,
                    "displayName": filename,
                    "fileType": file_type,
                    "mimeType": mime_type,
                    "path": str(artifact.path),
                    "size": artifact.size_bytes,
                    "url": f"/files/{artifact.public_alias}",
                })
            else:
                path = _EXPORT_DIR / filename
                path.write_text(text, encoding="utf-8")
                if getattr(session, "owner_id", ""):
                    from core.web_artifact_store import register_web_artifact
                    register_web_artifact("export", filename, session.owner_id, {
                        "fileName": filename, "displayName": filename,
                        "fileType": file_type, "mimeType": mime_type,
                    })
                out.append({
                    "fileName": filename,
                    "fileType": file_type,
                    "mimeType": mime_type,
                    "path": str(path),
                    "size": path.stat().st_size,
                })
        except OSError:
            continue
    return out
