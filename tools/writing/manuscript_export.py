"""Manuscript export (初稿/润色稿/修改清单 → md / docx / tex 下载文件).

The writing-side counterpart of tools/export/report.py (which exports
research artifacts). The agent passes the markdown content it produced;
we persist a file under data/exports/ and return a /files/ download record.

Markdown subset mapped for docx/tex: ATX headings (#..####), - / 1. lists,
**bold** inline, and GitHub pipe tables. LaTeX output uses ctexart so CJK
content compiles out of the box on a full TeX Live.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_EXPORT_DIR = Path(__file__).resolve().parents[2] / "data" / "exports"

_MIME = {"md": "text/markdown", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "tex": "application/x-tex"}
_FTYPE = {"md": "text", "docx": "word", "tex": "text"}


def _slug(text: str) -> str:
    kept = re.sub(r"[^\w一-鿿-]+", "_", (text or "").strip(), flags=re.UNICODE)
    return kept[:40].strip("_") or "manuscript"


def _split_inline_bold(text: str) -> list[tuple[str, bool]]:
    """[(chunk, is_bold)] splitting on **bold** markers."""
    out: list[tuple[str, bool]] = []
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if part:
            out.append((part, i % 2 == 1))
    return out


def _tex_escape(s: str) -> str:
    return (s.replace("\\", r"\textbackslash{}")
             .replace("&", r"\&").replace("%", r"\%").replace("$", r"\$")
             .replace("#", r"\#").replace("_", r"\_")
             .replace("{", r"\{").replace("}", r"\}"))


def _to_tex(title: str, lines: list[str]) -> str:
    body: list[str] = []
    in_list = False
    for ln in lines:
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            if in_list:
                body.append("\\end{itemize}"); in_list = False
            cmd = {1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph"}[len(m.group(1))]
            body.append(f"\\{cmd}{{{_tex_escape(m.group(2))}}}")
            continue
        m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)", ln)
        if m:
            if not in_list:
                body.append("\\begin{itemize}"); in_list = True
            body.append("\\item " + _tex_inline(m.group(1)))
            continue
        if in_list:
            body.append("\\end{itemize}"); in_list = False
        if ln.strip() and not ln.strip().startswith("|"):
            body.append(_tex_inline(ln))
    if in_list:
        body.append("\\end{itemize}")
    return ("\\documentclass[12pt]{ctexart}\n\\usepackage[margin=2.5cm]{geometry}\n"
            f"\\title{{{_tex_escape(title)}}}\n\\date{{}}\n\\begin{{document}}\n\\maketitle\n"
            + "\n\n".join(body) + "\n\\end{document}\n")


def _tex_inline(s: str) -> str:
    return "".join(f"\\textbf{{{_tex_escape(c)}}}" if b else _tex_escape(c)
                   for c, b in _split_inline_bold(s))


def _to_docx(title: str, lines: list[str], path: Path) -> None:
    import docx
    from docx.shared import Pt

    doc = docx.Document()
    doc.add_heading(title or "manuscript", level=0)
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            doc.add_heading(m.group(2).strip(), level=min(len(m.group(1)), 4))
            i += 1
            continue
        m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)", ln)
        if m:
            p = doc.add_paragraph(style="List Bullet")
            for chunk, bold in _split_inline_bold(m.group(1)):
                run = p.add_run(chunk)
                run.bold = bold
            i += 1
            continue
        if ln.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            if rows:
                table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
                table.style = "Table Grid"
                for r, row in enumerate(rows):
                    for c, cell in enumerate(row):
                        table.cell(r, c).text = cell
            continue
        if ln.strip():
            p = doc.add_paragraph()
            for chunk, bold in _split_inline_bold(ln):
                run = p.add_run(chunk)
                run.bold = bold
                run.font.size = Pt(11)
        i += 1
    doc.save(str(path))


def export_manuscript(
    title: str, content: str, fmt: str = "md", *, storage_context=None,
    session_id: str = "", owner_id: str = "",
) -> dict:
    """Persist `content` as a downloadable manuscript file. Returns the
    /files/ record dict (fileName/fileType/mimeType/size + relative url)."""
    fmt = (fmt or "md").lower()
    if fmt not in _MIME:
        fmt = "md"
    if storage_context is not None and storage_context.channel == "openai_api":
        storage_context.ensure_layout()
        target_dir = storage_context.temp_dir
    else:
        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        target_dir = _EXPORT_DIR
    name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_slug(title)}.{fmt}"
    fp = target_dir / name
    lines = (content or "").splitlines()
    if fmt == "md":
        fp.write_text(f"# {title}\n\n{content}", encoding="utf-8")
    elif fmt == "tex":
        fp.write_text(_to_tex(title, lines), encoding="utf-8")
    else:
        _to_docx(title, lines, fp)
    if storage_context is not None and storage_context.channel == "openai_api":
        from core.api_artifact_store import ApiArtifactStore
        from core.api_storage_store import ApiStorageStore
        artifact = ApiArtifactStore(ApiStorageStore(storage_context)).save_export(
            fp, session_id=session_id, display_name=name, mime_type=_MIME[fmt]
        )
        fp.unlink(missing_ok=True)
        return {
            "fileName": artifact.public_alias,
            "displayName": name,
            "fileType": _FTYPE[fmt], "mimeType": _MIME[fmt],
            "size": artifact.size_bytes, "url": f"/files/{artifact.public_alias}",
        }
    if owner_id:
        from core.web_artifact_store import register_web_artifact
        register_web_artifact("export", name, owner_id, {
            "fileName": name, "displayName": name,
            "fileType": _FTYPE[fmt], "mimeType": _MIME[fmt],
        })
    return {
        "fileName": name,
        "fileType": _FTYPE[fmt],
        "mimeType": _MIME[fmt],
        "size": fp.stat().st_size,
        "url": f"/files/{name}",
    }
