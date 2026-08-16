"""manuscript_export: md/docx/tex manuscript files + docx/tex upload parsing."""
import docx
import pytest

from tools.writing.manuscript_export import export_manuscript

CONTENT = """## 引言
这是**引言**段落，含加粗。

- 要点一
- 要点二

| 方法 | 指标 |
|---|---|
| A | 0.9 |

## 方法
方法内容。
"""


def test_export_md(tmp_path, monkeypatch):
    import tools.writing.manuscript_export as me
    monkeypatch.setattr(me, "_EXPORT_DIR", tmp_path)
    rec = export_manuscript("测试稿", CONTENT, "md")
    text = (tmp_path / rec["fileName"]).read_text(encoding="utf-8")
    assert text.startswith("# 测试稿")
    assert rec["fileType"] == "text"


def test_export_docx_roundtrip(tmp_path, monkeypatch):
    import tools.writing.manuscript_export as me
    monkeypatch.setattr(me, "_EXPORT_DIR", tmp_path)
    rec = export_manuscript("测试稿", CONTENT, "docx")
    doc = docx.Document(str(tmp_path / rec["fileName"]))
    heads = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert "引言" in heads and "方法" in heads
    assert rec["mimeType"].endswith("wordprocessingml.document")


def test_export_tex_structure(tmp_path, monkeypatch):
    import tools.writing.manuscript_export as me
    monkeypatch.setattr(me, "_EXPORT_DIR", tmp_path)
    rec = export_manuscript("测试稿", CONTENT, "tex")
    text = (tmp_path / rec["fileName"]).read_text(encoding="utf-8")
    assert "\\subsection{引言}" in text and "\\subsection{方法}" in text
    assert "\\begin{itemize}" in text and "\\item 要点一" in text
    assert "\\textbf{引言}段落" in text  # inline bold preserved
    assert "ctexart" in text


def test_export_invalid_format_falls_back_md(tmp_path, monkeypatch):
    import tools.writing.manuscript_export as me
    monkeypatch.setattr(me, "_EXPORT_DIR", tmp_path)
    rec = export_manuscript("x", CONTENT, "pdf")
    assert rec["fileName"].endswith(".md")


def test_docx_upload_parsing():
    """tools/ingest extracts docx text (chat upload path delegates here)."""
    import io
    d = docx.Document()
    d.add_heading("我的论文", level=1)
    d.add_paragraph("摘要内容")
    buf = io.BytesIO()
    d.save(buf)
    from tools.ingest.downloader import extract_text
    text = extract_text(buf.getvalue(), "docx")
    assert "我的论文" in text and "摘要内容" in text
    # tex decodes as plain text
    assert "\\section{引言}" in extract_text("\\section{引言}".encode(), "tex")
