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

def render_review_report(session: ChatSession) -> str:
    """文献综述 as a standalone markdown document."""
    title = session.topic or "未命名主题"
    return f"# 文献综述：{title}\n\n{session.literature_review}\n"


def write_reports(session: ChatSession, kinds: set[str]) -> list[dict]:
    """Render + persist the given artifact kinds. Returns file records:
    [{fileName, fileType, mimeType, path, size}]. Best-effort per kind."""
    api_context = session.storage_context if session.channel == "openai_api" else None
    if api_context is None:
        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    else:
        api_context.ensure_layout()
    out: list[dict] = []
    renders: list[tuple[str, str, str, str, str]] = []  # kind, filename, text, type, mime
    stamp = time.strftime("%Y%m%d_%H%M%S")

    if "research_map" in kinds and session.map_data:
        renders.append(("map", f"research_map_{stamp}_{uuid.uuid4().hex[:6]}.md",
                        render_research_report(session), "text", "text/markdown"))
        svg = render_research_map_svg(session)
        if svg:
            renders.append(("map_svg", f"research_graph_{stamp}_{uuid.uuid4().hex[:6]}.svg",
                            svg, "image", "image/svg+xml"))
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
