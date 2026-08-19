"""Markdown report rendering + file writing for x_soda.attachments output.

When a /v1 turn produced research artifacts (research map, reading path,
literature review), they are rendered into short-lived files under
data/exports/ (or API-private export storage) and served via GET /files/{name};
the file URLs go into x_soda.attachments so 清小搭 can hand them to the user.
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

_AGGREGATE_THRESHOLD = 3  # >2 papers in one (cluster, year) bucket collapse to "+N"


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


def build_research_map_view(session: ChatSession) -> dict:
    """Build the canonical, deterministic view consumed by every map format."""
    md = session.map_data or {}
    graph = md.get("graph") or {}
    raw_nodes = [
        n for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and str(n.get("id") or "").strip()
    ]
    nodes = sorted(
        [dict(n) for n in raw_nodes],
        key=lambda n: (
            _safe_int(n.get("cluster")), _safe_int(n.get("year")),
            str(n.get("title") or n.get("id") or "").casefold(), str(n.get("id")),
        ),
    )
    node_ids = {str(n["id"]) for n in nodes}
    edge_seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []
    for raw in graph.get("edges") or []:
        if not isinstance(raw, dict):
            continue
        source, target = str(raw.get("source") or ""), str(raw.get("target") or "")
        if source not in node_ids or target not in node_ids or source == target:
            continue
        kind = "semantic" if str(raw.get("type") or "") == "semantic" else "cites"
        key = (source, target, kind)
        if key in edge_seen:
            continue
        edge_seen.add(key)
        edges.append({"source": source, "target": target, "type": kind})
    edges.sort(key=lambda e: (e["type"], e["source"], e["target"]))
    clusters = {
        _safe_int(c.get("id")): {
            "id": _safe_int(c.get("id")),
            "label": _short_text(c.get("label") or f"主题簇 {_safe_int(c.get('id'))}", 80),
            "overview": " ".join(str(c.get("overview") or "").split()),
        }
        for c in (md.get("clusters") or []) if isinstance(c, dict)
    }
    for node in nodes:
        cluster_id = _safe_int(node.get("cluster"))
        clusters.setdefault(cluster_id, {
            "id": cluster_id, "label": f"主题簇 {cluster_id}", "overview": "",
        })
    clusters_list = [clusters[key] for key in sorted(clusters)]
    buckets: dict[tuple[int, int], list[dict]] = {}
    for node in nodes:
        buckets.setdefault((_safe_int(node.get("cluster")), _safe_int(node.get("year"))), []).append(node)
    aggregates = []
    aliases = {str(n["id"]): str(n["id"]) for n in nodes}
    for (cluster, year), members in sorted(buckets.items()):
        if len(members) > _AGGREGATE_THRESHOLD - 1:
            aggregate_id = f"aggregate:{cluster}:{year}"
            aggregates.append({
                "id": aggregate_id, "cluster": cluster, "year": year,
                "member_ids": [str(n["id"]) for n in members],
                "members": members,
            })
            for member in members:
                aliases[str(member["id"])] = aggregate_id
    return {
        "topic": str(session.topic or "未命名主题"),
        "landscape": str(md.get("landscape") or ""),
        "clusters": clusters_list,
        "nodes": nodes,
        "edges": edges,
        "aggregates": aggregates,
        "aliases": aliases,
    }


def _view_edge_aliases(view: dict) -> list[dict]:
    """Map edges through aggregate nodes and deduplicate them."""
    aliases = view["aliases"]
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for edge in view["edges"]:
        source, target = aliases.get(edge["source"], edge["source"]), aliases.get(edge["target"], edge["target"])
        kind = edge["type"]
        key = (source, target, kind)
        if source == target or key in seen:
            continue
        seen.add(key)
        out.append({"source": source, "target": target, "type": kind})
    return sorted(out, key=lambda e: (e["type"], e["source"], e["target"]))


def render_pretty_research_map_svg(session: ChatSession) -> str:
    """Render a deterministic, self-contained SVG optimized for file cards.

    The document uses only SVG primitives and an internal stylesheet: no
    JavaScript, external fonts, external CSS, remote images, or filters. Dense
    (theme, year) buckets collapse deterministically and excess theme clusters
    share a final fallback lane, keeping a stable viewBox on narrow hosts.
    """
    import html
    import time as _time

    view = build_research_map_view(session)
    md, nodes, edges = view, view["nodes"], view["edges"]
    if not nodes:
        return ""

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


def _mermaid_id(value: str) -> str:
    import hashlib
    return "n_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _mermaid_label(value, limit: int = 72) -> str:
    import html
    text = " ".join(_short_text(value, limit).replace("`", "′").split())
    return html.escape(text, quote=True).replace("'", "&#39;")


def render_research_map_mermaid(session: ChatSession) -> str:
    """Return a deterministic, non-executable Mermaid source block."""
    view = build_research_map_view(session)
    if not view["nodes"]:
        return ""
    clusters = {c["id"]: c for c in view["clusters"]}
    aggregated_members = {member for a in view["aggregates"] for member in a["member_ids"]}
    lines = ["```mermaid", "flowchart LR"]
    for cluster_id in sorted(clusters):
        cluster = clusters[cluster_id]
        lines.append(f'  subgraph cluster_{cluster_id}["{_mermaid_label(cluster["label"], 50)}"]')
        for node in view["nodes"]:
            if _safe_int(node.get("cluster")) != cluster_id or str(node["id"]) in aggregated_members:
                continue
            nid = _mermaid_id(str(node["id"]))
            title = _mermaid_label(node.get("title") or node["id"], 58)
            meta = _mermaid_label(f'{node.get("year") or "n.d."} · 被引 {_safe_int(node.get("citation_count"))}', 30)
            lines.append(f'    {nid}["{title}<br/>{meta}"]')
        for aggregate in view["aggregates"]:
            if aggregate["cluster"] == cluster_id:
                aid = _mermaid_id(aggregate["id"])
                year = aggregate["year"] or "n.d."
                lines.append(f'    {aid}(["+{len(aggregate["members"])} 篇<br/>{year} 聚合节点"])')
        lines.append("  end")
    for edge in _view_edge_aliases(view):
        source, target = _mermaid_id(edge["source"]), _mermaid_id(edge["target"])
        lines.append(
            f"  {source} -. 语义 .-> {target}" if edge["type"] == "semantic"
            else f"  {source} -->|引用| {target}"
        )
    lines.extend([
        "  classDef foundational stroke-width:3px,stroke-dasharray:4 2;",
        "  classDef candidate stroke-dasharray:3 2;",
    ])
    foundational = [_mermaid_id(str(n["id"])) for n in view["nodes"]
                    if str(n["id"]) not in aggregated_members
                    and str(n.get("role") or "") == "foundational"]
    candidate = [_mermaid_id(str(n["id"])) for n in view["nodes"]
                 if str(n["id"]) not in aggregated_members
                 and str(n.get("layer") or "core") == "candidate"]
    if foundational:
        lines.append(f"  class {','.join(foundational)} foundational;")
    if candidate:
        lines.append(f"  class {','.join(candidate)} candidate;")
    lines.append("```")
    return "\n".join(lines)


def _html_json(value: object) -> str:
    import json
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def render_research_map_html(session: ChatSession) -> str:
    """Self-contained downloadable interactive map with no network access."""
    import html
    from urllib.parse import urlsplit

    view = build_research_map_view(session)
    if not view["nodes"]:
        return ""
    cluster_labels = {c["id"]: c["label"] for c in view["clusters"]}
    paper_by_id = {str(p.id): p for p in session.all_papers() if getattr(p, "id", None)}

    def source_url(node: dict) -> str:
        paper = paper_by_id.get(str(node.get("id") or ""))
        url = str(node.get("url") or "").strip()
        if not url and paper is not None and paper.urls:
            url = next(iter(paper.urls.values()), "")
        doi = str(node.get("doi") or getattr(paper, "doi", "") or "").strip()
        if not url and doi:
            url = f"https://doi.org/{doi.removeprefix('https://doi.org/').removeprefix('doi:')}"
        parsed = urlsplit(url)
        return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""

    aggregated_members = {m for a in view["aggregates"] for m in a["member_ids"]}
    data_nodes = [{
        "id": str(n["id"]), "title": str(n.get("title") or n["id"]),
        "year": n.get("year") or None, "cluster": _safe_int(n.get("cluster")),
        "clusterLabel": cluster_labels.get(_safe_int(n.get("cluster")), ""),
        "citations": _safe_int(n.get("citation_count")), "role": str(n.get("role") or ""),
        "layer": str(n.get("layer") or "core"),
        "doi": str(n.get("doi") or getattr(paper_by_id.get(str(n["id"])), "doi", "") or ""),
        "url": source_url(n), "members": [],
    } for n in view["nodes"] if str(n["id"]) not in aggregated_members]
    for aggregate in view["aggregates"]:
        data_nodes.append({
            "id": aggregate["id"], "title": f'+{len(aggregate["members"])} 篇聚合论文',
            "year": aggregate["year"] or None, "cluster": aggregate["cluster"],
            "clusterLabel": cluster_labels.get(aggregate["cluster"], ""),
            "citations": sum(_safe_int(n.get("citation_count")) for n in aggregate["members"]),
            "role": "aggregate", "layer": "aggregate", "doi": "", "url": "",
            "members": [{"id": str(n["id"]), "title": str(n.get("title") or n["id"]),
                         "year": n.get("year") or None,
                         "citations": _safe_int(n.get("citation_count")),
                         "doi": str(n.get("doi") or getattr(paper_by_id.get(str(n["id"])), "doi", "") or ""),
                         "url": source_url(n)} for n in aggregate["members"]],
        })
    payload = {"topic": view["topic"], "clusters": view["clusters"],
               "nodes": data_nodes, "edges": _view_edge_aliases(view)}
    controls = "".join(
        f'<label><input class="cluster-filter" type="checkbox" value="{c["id"]}" checked> '
        f'{html.escape(c["label"])}</label>' for c in view["clusters"]
    )
    title, data = html.escape(view["topic"]), _html_json(payload)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; font-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'">
<title>{title} · 研究图谱</title><style>
:root{{--bg:#f7f5ef;--panel:#fff;--ink:#17212b;--muted:#667085;--accent:#256d66}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px system-ui,sans-serif}}header{{padding:16px 20px;background:var(--panel);border-bottom:1px solid #ddd}}h1{{font-size:18px;margin:0 0 6px}}main{{display:grid;grid-template-columns:230px 1fr 280px;height:calc(100vh - 72px)}}aside{{padding:16px;background:var(--panel);overflow:auto}}#controls{{border-right:1px solid #ddd}}#details{{border-left:1px solid #ddd}}label{{display:block;margin:8px 0}}#stage{{overflow:hidden;touch-action:none;cursor:grab}}#stage.dragging{{cursor:grabbing}}svg{{width:100%;height:100%;background:#fbfaf7}}.edge{{fill:none;stroke:#94a3b8;stroke-width:1.4}}.semantic{{stroke-dasharray:6 5}}.node rect{{fill:#fff;stroke:var(--accent);stroke-width:1.5}}.node.aggregate rect{{fill:#256d66}}.node.aggregate text{{fill:#fff}}.node text{{pointer-events:none;font-size:12px}}button{{padding:6px 10px;border:1px solid #bbb;border-radius:7px;background:#fff}}ul{{padding-left:18px}}@media(max-width:850px){{main{{grid-template-columns:1fr}}aside{{display:none}}}}
</style></head><body><header><h1>{title}</h1><div>下载后的自包含交互图谱 · 不访问网络或浏览器存储</div></header><main>
<aside id="controls"><strong>主题簇筛选</strong>{controls}<hr><label><input id="show-cites" type="checkbox" checked> 引用边</label><label><input id="show-semantic" type="checkbox" checked> 语义边</label><button id="reset" type="button">重置视图</button></aside>
<section id="stage" aria-label="研究图谱画布"><svg viewBox="0 0 1200 760"><g id="viewport"></g></svg></section><aside id="details"><strong>节点详情</strong><p>点击论文或聚合节点查看详情。</p></aside></main>
<script id="map-data" type="application/json">{data}</script><script>
'use strict';const D=JSON.parse(document.getElementById('map-data').textContent);const V=document.getElementById('viewport'),S=document.querySelector('#stage svg'),P=document.getElementById('stage'),Q=document.getElementById('details');let z=1,tx=0,ty=0,drag=null;
const active=()=>new Set([...document.querySelectorAll('.cluster-filter:checked')].map(x=>Number(x.value)));function transform(){{V.setAttribute('transform',`translate(${{tx}} ${{ty}}) scale(${{z}})`);}}function el(n,a={{}},t=''){{const x=document.createElementNS('http://www.w3.org/2000/svg',n);for(const[k,v]of Object.entries(a))x.setAttribute(k,v);x.textContent=t;return x;}}
function link(url,label){{if(!url)return;const a=document.createElement('a');a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.textContent=label;Q.append(a);}}function show(n){{Q.replaceChildren();const h=document.createElement('h2');h.textContent=n.title;Q.append(h);for(const [k,v] of [['主题簇',n.clusterLabel],['年份',n.year||'n.d.'],['被引',n.citations],['角色',n.role||'—'],['DOI',n.doi||'—']]){{const p=document.createElement('p');p.textContent=`${{k}}：${{v}}`;Q.append(p);}}link(n.url,'打开 DOI / 来源');if(n.members.length){{const h3=document.createElement('h3');h3.textContent='聚合成员';Q.append(h3);const ul=document.createElement('ul');n.members.forEach(m=>{{const li=document.createElement('li');li.textContent=`${{m.title}} (${{m.year||'n.d.'}}, 被引 ${{m.citations}}${{m.doi?' · DOI '+m.doi:''}})`;ul.append(li);}});Q.append(ul);}}}}
function render(){{V.replaceChildren();const A=active(),nodes=D.nodes.filter(n=>A.has(n.cluster)),ids=new Set(nodes.map(n=>n.id)),pos=new Map();const lanes=[...A].sort((a,b)=>a-b);nodes.sort((a,b)=>(a.year||0)-(b.year||0)||a.title.localeCompare(b.title));nodes.forEach((n,i)=>{{const x=130+(i%6)*175,y=90+lanes.indexOf(n.cluster)*125+(Math.floor(i/6)%2)*45;pos.set(n.id,[x,y]);}});for(const e of D.edges){{if(!ids.has(e.source)||!ids.has(e.target)||(!document.getElementById('show-cites').checked&&e.type==='cites')||(!document.getElementById('show-semantic').checked&&e.type==='semantic'))continue;const a=pos.get(e.source),b=pos.get(e.target),l=el('line',{{x1:a[0],y1:a[1],x2:b[0],y2:b[1],class:`edge ${{e.type==='semantic'?'semantic':''}}`}});V.append(l);}}for(const n of nodes){{const [x,y]=pos.get(n.id),g=el('g',{{class:`node ${{n.layer==='aggregate'?'aggregate':''}}`,tabindex:'0',role:'button'}});g.append(el('rect',{{x:x-70,y:y-24,width:140,height:48,rx:10}}));g.append(el('text',{{x,y:y-3,'text-anchor':'middle'}},n.title.slice(0,20)));g.append(el('text',{{x,y:y+14,'text-anchor':'middle'}},`${{n.year||'n.d.'}} · 被引 ${{n.citations}}`));g.addEventListener('click',()=>show(n));g.addEventListener('keydown',e=>{{if(e.key==='Enter'||e.key===' ')show(n);}});V.append(g);}}transform();}}
document.querySelectorAll('input').forEach(x=>x.addEventListener('change',render));document.getElementById('reset').addEventListener('click',()=>{{z=1;tx=0;ty=0;transform();}});S.addEventListener('wheel',e=>{{e.preventDefault();z=Math.max(.35,Math.min(3,z*(e.deltaY<0?1.12:.89)));transform();}},{{passive:false}});P.addEventListener('pointerdown',e=>{{drag=[e.clientX-tx,e.clientY-ty];P.classList.add('dragging');P.setPointerCapture(e.pointerId);}});P.addEventListener('pointermove',e=>{{if(drag){{tx=e.clientX-drag[0];ty=e.clientY-drag[1];transform();}}}});P.addEventListener('pointerup',()=>{{drag=null;P.classList.remove('dragging');}});render();
</script></body></html>'''


def render_pretty_research_map_markdown(session: ChatSession) -> str:
    """Portable relation listing generated from the canonical graph view."""
    from urllib.parse import urlsplit

    view = build_research_map_view(session)
    nodes, edges = view["nodes"], view["edges"]
    node_by_id = {str(n["id"]): n for n in nodes}
    cluster_labels = {c["id"]: c["label"] for c in view["clusters"]}
    paper_by_id = {str(p.id): p for p in session.all_papers() if getattr(p, "id", None)}

    def paper_link(node: dict) -> str:
        paper = paper_by_id.get(str(node.get("id") or ""))
        doi = str(node.get("doi") or getattr(paper, "doi", "") or "").strip()
        url = str(node.get("url") or "").strip()
        if not url and paper is not None and paper.urls:
            url = next(iter(paper.urls.values()), "")
        if not url and doi:
            clean = doi.removeprefix("https://doi.org/").removeprefix("doi:")
            url = f"https://doi.org/{clean}"
        parsed = urlsplit(url)
        safe_url = url if parsed.scheme in {"http", "https"} and parsed.netloc else ""
        label = f"DOI: {doi}" if doi else "来源"
        return f"[{_markdown_cell(label)}]({safe_url})" if safe_url else _markdown_cell(doi or "—")

    parts = [f"# 引用关系说明：{_markdown_cell(view['topic'])}", "",
             "> 本文件是研究图谱的可复制关系说明，不嵌入 Mermaid 或可执行代码。", "",
    ]
    if view["landscape"]:
        parts.extend(["## 领域脉络", "", _markdown_cell(view["landscape"]), ""])
    parts.extend(["## 图例", "", "- **直接引用**：实线关系。", "- **语义关联**：虚线关系。",
             "- **奠基性论文**：角色为 foundational。", "- **候选层**：节点 layer 为 candidate。",
             "- **聚合节点**：同一主题簇、同一年超过 2 篇时折叠；下方列出全部成员。", "",
             "## 主题簇摘要", ""])
    for cluster in view["clusters"]:
        parts.append(f"- **{_markdown_cell(cluster['label'])}**：{_markdown_cell(cluster['overview'] or '暂无摘要')}")
    parts.extend(["", "## 论文清单", "", "| 年份 | 主题簇 | 论文 | 层级 / 角色 | 被引 | DOI / 来源 |", "|---:|---|---|---|---:|---|"])
    for node in nodes:
        cluster = _safe_int(node.get("cluster"))
        layer_role = " / ".join(x for x in [str(node.get("layer") or "core"), str(node.get("role") or "")] if x)
        parts.append(f"| {_markdown_cell(node.get('year') or 'n.d.')} | {_markdown_cell(cluster_labels.get(cluster))} | {_markdown_cell(node.get('title') or node['id'])} | {_markdown_cell(layer_role)} | {_safe_int(node.get('citation_count'))} | {paper_link(node)} |")
    parts.extend(["", "## 引用与语义关系", ""])
    if not edges:
        parts.append("暂无关系边。")
    else:
        parts.extend(["| 类型 | 起点 | 终点 |", "|---|---|---|"])
        for edge in edges:
            relation = "语义关联" if edge["type"] == "semantic" else "直接引用"
            parts.append(f"| {relation} | {_markdown_cell(node_by_id[edge['source']].get('title') or edge['source'])} | {_markdown_cell(node_by_id[edge['target']].get('title') or edge['target'])} |")
    if view["aggregates"]:
        parts.extend(["", "## 聚合节点展开", ""])
        for aggregate in view["aggregates"]:
            label = cluster_labels.get(aggregate["cluster"], f"主题簇 {aggregate['cluster']}")
            parts.append(f"### {_markdown_cell(label)} · {aggregate['year'] or 'n.d.'}（{len(aggregate['members'])} 篇）")
            parts.extend(f"- {_markdown_cell(member.get('title') or member['id'])}" for member in aggregate["members"])
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_review_report(session: ChatSession) -> str:
    """文献综述 as a standalone markdown document."""
    title = session.topic or "未命名主题"
    return f"# 文献综述：{title}\n\n{session.literature_review}\n"


def write_reports(
    session: ChatSession, kinds: set[str], *,
    research_map_svg_enabled: bool = True,
    research_map_html_enabled: bool = False,
    research_map_markdown_enabled: bool = True,
) -> list[dict]:
    """Render file artifacts; Mermaid is content, never an attachment.

    Explicit/library export defaults to SVG + Markdown and therefore remains
    independent of the administrator's /v1 display policy.
    """
    toggles = {
        "research_map_svg_enabled": research_map_svg_enabled,
        "research_map_html_enabled": research_map_html_enabled,
        "research_map_markdown_enabled": research_map_markdown_enabled,
    }
    for field, value in toggles.items():
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be a boolean")
    api_context = session.storage_context if session.channel == "openai_api" else None
    if api_context is None:
        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    else:
        api_context.ensure_layout()
    out: list[dict] = []
    renders: list[tuple[str, str, str, str, str]] = []
    stamp = time.strftime("%Y%m%d_%H%M%S")

    if "research_map" in kinds and session.map_data:
        if research_map_markdown_enabled:
            renders.append(("map", f"research_map_relations_{stamp}_{uuid.uuid4().hex[:6]}.md",
                            render_pretty_research_map_markdown(session), "text", "text/markdown"))
        if research_map_svg_enabled:
            svg = render_pretty_research_map_svg(session)
            if svg:
                renders.append(("map_svg", f"research_graph_{stamp}_{uuid.uuid4().hex[:6]}.svg",
                                svg, "image", "image/svg+xml"))
        if research_map_html_enabled:
            html_doc = render_research_map_html(session)
            if html_doc:
                renders.append(("map_html", f"research_graph_{stamp}_{uuid.uuid4().hex[:6]}.html",
                                html_doc, "text", "text/html"))
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
                    temp, session_id=session.session_id, display_name=filename, mime_type=mime_type)
                temp.unlink(missing_ok=True)
                out.append({"fileName": artifact.public_alias, "displayName": filename,
                            "fileType": file_type, "mimeType": mime_type, "path": str(artifact.path),
                            "size": artifact.size_bytes, "url": f"/files/{artifact.public_alias}"})
            else:
                path = _EXPORT_DIR / filename
                path.write_text(text, encoding="utf-8")
                if getattr(session, "owner_id", ""):
                    from core.web_artifact_store import register_web_artifact
                    register_web_artifact("export", filename, session.owner_id, {
                        "fileName": filename, "displayName": filename,
                        "fileType": file_type, "mimeType": mime_type})
                out.append({"fileName": filename, "fileType": file_type, "mimeType": mime_type,
                            "path": str(path), "size": path.stat().st_size})
        except OSError:
            continue
    return out
