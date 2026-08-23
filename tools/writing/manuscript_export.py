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
    list_env: str | None = None

    def close_list() -> None:
        nonlocal list_env
        if list_env:
            body.append(f"\\end{{{list_env}}}")
            list_env = None

    for ln in lines:
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            close_list()
            cmd = {1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph"}[len(m.group(1))]
            body.append(f"\\{cmd}{{{_tex_escape(m.group(2))}}}")
            continue
        m = re.match(r"^\s*([-*]|\d+\.)\s+(.*)", ln)
        if m:
            wanted = "enumerate" if m.group(1)[0].isdigit() else "itemize"
            if list_env != wanted:
                close_list()
                body.append(f"\\begin{{{wanted}}}")
                list_env = wanted
            body.append("\\item " + _tex_inline(m.group(2)))
            continue
        close_list()
        if ln.strip() and not ln.strip().startswith("|"):
            body.append(_tex_inline(ln))
    close_list()
    return ("\\documentclass[12pt]{ctexart}\n\\usepackage[margin=2.5cm]{geometry}\n"
            f"\\title{{{_tex_escape(title)}}}\n\\date{{}}\n\\begin{{document}}\n\\maketitle\n"
            + "\n\n".join(body) + "\n\\end{document}\n")


def _tex_inline(s: str) -> str:
    return "".join(f"\\textbf{{{_tex_escape(c)}}}" if b else _tex_escape(c)
                   for c, b in _split_inline_bold(s))


def _add_markdown_runs(paragraph, text: str, doc) -> None:
    """Render the small Markdown inline subset used by generated reports."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    token = re.compile(r"(\*\*.+?\*\*|\[[^\]]+\]\(https?://[^)]+\))")

    def add_hyperlink(label: str, url: str) -> None:
        relationship = doc.part.relate_to(url, RT.HYPERLINK, is_external=True)
        hyperlink = OxmlElement("w:hyperlink")
        hyperlink.set(qn("r:id"), relationship)
        run = OxmlElement("w:r")
        properties = OxmlElement("w:rPr")
        color = OxmlElement("w:color")
        color.set(qn("w:val"), "0563C1")
        properties.append(color)
        underline = OxmlElement("w:u")
        underline.set(qn("w:val"), "single")
        properties.append(underline)
        run.append(properties)
        value = OxmlElement("w:t")
        value.text = label
        run.append(value)
        hyperlink.append(run)
        paragraph._p.append(hyperlink)

    for part in token.split(text or ""):
        if not part:
            continue
        link = re.fullmatch(r"\[([^]]+)\]\((https?://[^)]+)\)", part)
        if link:
            add_hyperlink(link.group(1), link.group(2))
            continue
        bold = re.fullmatch(r"\*\*(.+?)\*\*", part)
        run = paragraph.add_run(bold.group(1) if bold else part)
        run.bold = bool(bold)


def _split_table_row(line: str) -> list[str]:
    r"""Split a GitHub-style table row while preserving escaped ``\|`` text."""
    body = line.strip().strip("|")
    return [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", body)]


def _to_docx(title: str, lines: list[str], path: Path, *, include_title: bool = True) -> None:
    import docx
    from docx.shared import Pt

    doc = docx.Document()
    if include_title:
        doc.add_heading(title or "manuscript", level=0)
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            doc.add_heading(m.group(2).strip(), level=min(len(m.group(1)), 4))
            i += 1
            continue
        m = re.match(r"^\s*([-*]|\d+\.)\s+(.*)", ln)
        if m:
            style = "List Number" if m.group(1)[0].isdigit() else "List Bullet"
            p = doc.add_paragraph(style=style)
            _add_markdown_runs(p, m.group(2), doc)
            i += 1
            continue
        if ln.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i]):
                    rows.append(_split_table_row(lines[i]))
                i += 1
            if rows:
                table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
                table.style = "Table Grid"
                for r, row in enumerate(rows):
                    for c, cell in enumerate(row):
                        paragraph = table.cell(r, c).paragraphs[0]
                        _add_markdown_runs(paragraph, cell, doc)
            continue
        if ln.strip():
            p = doc.add_paragraph()
            _add_markdown_runs(p, ln, doc)
            for run in p.runs:
                run.font.size = Pt(11)
        i += 1
    doc.save(str(path))


def export_manuscript(
    title: str, content: str, fmt: str = "md", *, storage_context=None,
    session_id: str = "", owner_id: str = "", include_title: bool = True,
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
        prefix = f"# {title}\n\n" if include_title else ""
        fp.write_text(prefix + content, encoding="utf-8")
    elif fmt == "tex":
        fp.write_text(_to_tex(title, lines), encoding="utf-8")
    else:
        _to_docx(title, lines, fp, include_title=include_title)
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
