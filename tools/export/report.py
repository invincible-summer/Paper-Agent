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



def render_research_map_svg(session: ChatSession) -> str:
    """Render the research-map graph as a portable, static SVG attachment.

    The self-hosted frontend keeps the fully interactive React graph. OpenAI-
    compatible hosts (including 清小搭) cannot execute that component, so this
    deterministic SVG provides the same nodes/edges as an ordinary image card.
    """
    import html

    md = session.map_data or {}
    graph = md.get("graph") or {}
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    if not nodes:
        return ""
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    years = sorted({int(n.get("year") or 0) for n in nodes})
    clusters = sorted({int(n.get("cluster") or 0) for n in nodes})
    year_pos = {year: i for i, year in enumerate(years)}
    cluster_pos = {cluster: i for i, cluster in enumerate(clusters)}
    width = max(900, 180 + max(1, len(years) - 1) * 120)
    height = max(420, 150 + max(1, len(clusters)) * 110)
    left, top = 90, 80
    x_step = (width - 180) / max(1, len(years) - 1)
    y_step = (height - 170) / max(1, len(clusters))
    positions = {
        str(n["id"]): (
            left + year_pos[int(n.get("year") or 0)] * x_step,
            top + (cluster_pos[int(n.get("cluster") or 0)] + 0.5) * y_step,
        )
        for n in nodes
    }
    colors = ["#256d66", "#c2402a", "#4a628a", "#8a6d3b", "#6b4a8a", "#3b7a4a"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfaf7"/>',
        '<style>text{font-family:Arial,"Noto Sans CJK SC",sans-serif}.axis{fill:#6b7280;font-size:12px}.label{fill:#24303f;font-size:11px}.title{fill:#17212b;font-size:20px;font-weight:700}.edge{stroke:#94a3b8;stroke-width:1.2;opacity:.55}.semantic{stroke-dasharray:5 4;opacity:.35}</style>',
        f'<text x="{left}" y="36" class="title">{html.escape(session.topic or "研究谱系图")}</text>',
    ]
    for year, idx in year_pos.items():
        x = left + idx * x_step
        parts.append(f'<line x1="{x:.1f}" y1="55" x2="{x:.1f}" y2="{height-55}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{x:.1f}" y="70" text-anchor="middle" class="axis">{year or "n.d."}</text>')
    for edge in edges:
        source, target = positions.get(str(edge.get("source"))), positions.get(str(edge.get("target")))
        if not source or not target:
            continue
        klass = "edge semantic" if edge.get("type") == "semantic" else "edge"
        parts.append(f'<line x1="{source[0]:.1f}" y1="{source[1]:.1f}" x2="{target[0]:.1f}" y2="{target[1]:.1f}" class="{klass}"/>')
    for node in nodes:
        x, y = positions[str(node["id"])]
        color = colors[int(node.get("cluster") or 0) % len(colors)]
        title = str(node.get("title") or node["id"])
        label = title[:24] + ("…" if len(title) > 24 else "")
        radius = 8 if node.get("role") == "foundational" else 6
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{color}"><title>{html.escape(title)}</title></circle>')
        parts.append(f'<text x="{x+10:.1f}" y="{y+4:.1f}" class="label">{html.escape(label)}</text>')
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
