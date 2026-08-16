"""Deterministic draft structure checker (论文结构体检) — pure Python, zero LLM.

Like the genealogy graph, the facts here must be COMPUTED, not imagined:
the agent gets a fact sheet (section tree, IMRaD completeness, balance
ratios, abstract length, citation hygiene, figure/table mentions) and then
does the qualitative advising itself (draft_review skill).

Input is the plain text extracted from the user's uploaded draft
(data/uploads/{id}.txt), so heading detection handles markdown, numbered
("2.1 Method"), and Chinese ("一、引言" / "第三章 方法") styles.
"""
from __future__ import annotations

import re

# Canonical IMRaD categories -> compiled title patterns (EN + ZH).
_CATEGORIES: dict[str, re.Pattern] = {
    "摘要": re.compile(r"abstract|摘要|摘 要", re.I),
    "引言": re.compile(r"introduction|引言|绪论|前言", re.I),
    "相关工作": re.compile(r"related\s*work|相关工作|文献综述|研究现状|国内外研究", re.I),
    "方法": re.compile(r"method|methodology|方法|模型|approach|系统设计|算法", re.I),
    "实验": re.compile(r"experiment|evaluation|实验|评估|结果与分析|results", re.I),
    "讨论": re.compile(r"discussion|讨论", re.I),
    "结论": re.compile(r"conclusion|结论|总结|结束语", re.I),
    "参考文献": re.compile(r"references|参考文献|bibliography|致谢", re.I),
}
# Severity when a category is missing entirely.
_MISSING_SEVERITY = {
    "摘要": "high", "引言": "high", "方法": "high", "结论": "high",
    "参考文献": "high", "实验": "medium", "相关工作": "medium", "讨论": "low",
}

_HEAD_MD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_HEAD_NUM = re.compile(r"^(\d+(?:\.\d+)*)[.、\s]\s+(\S.{0,80})$")
_HEAD_ZH = re.compile(r"^(第[一二三四五六七八九十百\d]+[章节]|[一二三四五六七八九十]+、)\s*(.{0,60})$")
_HEAD_LATEX = re.compile(
    r"^\\(chapter|section|subsection|subsubsection)\*?\{(.{1,80}?)\}", re.I)
_LATEX_ENV_HEADS = (  # environment openers that act as section heads
    (re.compile(r"^\\begin\{abstract\}", re.I), "Abstract"),
    (re.compile(r"^\\begin\{thebibliography\}", re.I), "References"),
    (re.compile(r"^\\bibliography\{", re.I), "References"),
)
_NUM_CITE = re.compile(r"\[(\d+(?:\s*[-–,]\s*\d+)*)\]")
_AUTHOR_YEAR_CITE = re.compile(
    r"\([A-Z][A-Za-z\-]+(?:(?:\s+et\s+al\.?)|(?:\s+and\s+[A-Z][A-Za-z\-]+))?,?\s+\d{4}[a-z]?\)"
    r"|[A-Z][A-Za-z\-]+\s+(?:et\s+al\.?\s+)?\(\d{4}[a-z]?\)")
_REF_ENTRY = re.compile(r"^\s*(?:\[(\d+)\]|(\d{1,2})[.、\)])\s+\S")
_FIGURE = re.compile(r"图\s*\d+|figure\s*\d+|fig\.\s*\d+", re.I)
_TABLE = re.compile(r"表\s*\d+|table\s*\d+", re.I)

# Balance guidelines (share of body characters, references excluded).
_BALANCE = {
    "引言": (0.40, "medium", "引言占比 {share:.0%} 偏高（参考上限 40%），考虑压缩背景、把细节移到相关工作。"),
    "方法": (0.08, "medium", "方法部分占比仅 {share:.0%}（参考下限 8%），技术细节可能不足以支撑复现。"),
    "实验": (0.15, "low", "实验部分占比 {share:.0%} 偏低（参考下限 15%），审稿人通常期待更充分的验证。"),
}


def _detect_heading(line: str) -> tuple[int, str] | None:
    """Return (level, title) if the line looks like a section heading."""
    m = _HEAD_MD.match(line)
    if m:
        return len(m.group(1)), m.group(2).strip()
    m = _HEAD_LATEX.match(line)
    if m:
        level = {"chapter": 1, "section": 1, "subsection": 2,
                 "subsubsection": 3}[m.group(1).lower()]
        return level, m.group(2).strip()
    for pat, title in _LATEX_ENV_HEADS:
        if pat.match(line):
            return 1, title
    m = _HEAD_NUM.match(line)
    if m:
        depth = m.group(1).count(".") + 1
        return min(depth, 6), m.group(2).strip()
    m = _HEAD_ZH.match(line)
    if m and m.group(2).strip():
        return 1, m.group(2).strip()
    # Bare category-only line (common in PDF-extracted text): "参考文献", "Abstract".
    if len(line) <= 15 and _category_of(line):
        return 1, line
    return None


def _category_of(title: str) -> str | None:
    for cat, pat in _CATEGORIES.items():
        if pat.search(title):
            return cat
    return None


def _word_count(text: str) -> int:
    """EN words + CJK chars (each CJK char ≈ one word)."""
    latin = len(re.findall(r"[A-Za-z0-9]+", text))
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return latin + cjk


def analyze_draft(text: str, filename: str = "") -> dict:
    """Analyze one draft's plain text; returns the fact sheet + markdown report."""
    lines = text.splitlines()

    # 1) Section map with per-section character counts.
    sections: list[dict] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or len(line) > 90:
            continue
        hit = _detect_heading(line)
        if hit:
            level, title = hit
            sections.append({"title": title, "level": level, "line": i, "chars": 0})
    for j, sec in enumerate(sections):
        end = sections[j + 1]["line"] if j + 1 < len(sections) else len(lines)
        sec["chars"] = sum(len(l) for l in lines[sec["line"] + 1:end])

    # 2) IMRaD completeness.
    found: dict[str, dict] = {}
    for sec in sections:
        cat = _category_of(sec["title"])
        if cat and cat not in found:
            found[cat] = sec
    missing = [c for c in _CATEGORIES if c not in found]

    # 3) Citation hygiene.
    body_end = found["参考文献"]["line"] if "参考文献" in found else len(lines)
    body_text = "\n".join(lines[:body_end])
    ref_text = "\n".join(lines[body_end:]) if "参考文献" in found else ""
    num_marks: set[int] = set()
    for grp in _NUM_CITE.findall(body_text):
        for part in re.split(r"[,，]", grp):
            part = part.strip()
            rng = re.split(r"[-–]", part)
            if len(rng) == 2 and rng[0].strip().isdigit() and rng[1].strip().isdigit():
                num_marks.update(range(int(rng[0]), int(rng[1]) + 1))
            elif part.isdigit():
                num_marks.add(int(part))
    author_year_marks = len(_AUTHOR_YEAR_CITE.findall(body_text))
    ref_entries: set[int] = set()
    for rl in ref_text.splitlines():
        m = _REF_ENTRY.match(rl)
        if m:
            n = m.group(1) or m.group(2)
            if n and n.isdigit():
                ref_entries.add(int(n))
    uncited_refs = sorted(ref_entries - num_marks) if num_marks else []

    # 4) Metrics.
    abstract_sec = found.get("摘要")
    abstract_text = ""
    if abstract_sec:
        idx = sections.index(abstract_sec)
        end = sections[idx + 1]["line"] if idx + 1 < len(sections) else len(lines)
        abstract_text = "\n".join(lines[abstract_sec["line"] + 1:end])
    body_chars = sum(s["chars"] for s in sections if s["line"] < body_end) or 1
    cat_share = {c: sec["chars"] / body_chars for c, sec in found.items() if sec["line"] < body_end}
    metrics = {
        "total_chars": len(text),
        "section_count": len(sections),
        "abstract_words": _word_count(abstract_text) if abstract_sec else 0,
        "numeric_citations": len(num_marks),
        "author_year_citations": author_year_marks,
        "reference_entries": len(ref_entries),
        "uncited_references": uncited_refs[:20],
        "figure_mentions": len(_FIGURE.findall(text)),
        "table_mentions": len(_TABLE.findall(text)),
        "category_share": {c: round(s, 3) for c, s in sorted(cat_share.items())},
    }

    # 5) Issues (deterministic, severity-tagged; thresholds marked as guidelines).
    issues: list[dict] = []
    for cat in missing:
        issues.append({"severity": _MISSING_SEVERITY.get(cat, "low"), "code": "missing_section",
                       "message": f"缺少「{cat}」章节", "evidence": "全文章节标题中未检测到"})
    if abstract_sec and 0 < metrics["abstract_words"] < 80:
        issues.append({"severity": "medium", "code": "abstract_short",
                       "message": f"摘要过短（约 {metrics['abstract_words']} 词/字）",
                       "evidence": "常规要求 150 词（英文）或 200 字（中文）以上"})
    elif metrics["abstract_words"] > 600:
        issues.append({"severity": "low", "code": "abstract_long",
                       "message": f"摘要偏长（约 {metrics['abstract_words']} 词/字）",
                       "evidence": "多数 venue 上限 250-300 词"})
    if "引言" in cat_share and cat_share["引言"] > _BALANCE["引言"][0]:
        thr, sev, tpl = _BALANCE["引言"]
        issues.append({"severity": sev, "code": "intro_heavy",
                       "message": tpl.format(share=cat_share["引言"]), "evidence": "字符占比"})
    for cat in ("方法", "实验"):
        if cat in found and cat_share.get(cat, 1) < _BALANCE[cat][0]:
            thr, sev, tpl = _BALANCE[cat]
            issues.append({"severity": sev, "code": f"{cat}_thin",
                           "message": tpl.format(share=cat_share.get(cat, 0)), "evidence": "字符占比"})
    if ref_entries and num_marks and uncited_refs:
        issues.append({"severity": "medium", "code": "uncited_refs",
                       "message": f"{len(uncited_refs)} 条参考文献疑似从未在正文中被引用",
                       "evidence": "编号 " + "、".join(f"[{n}]" for n in uncited_refs[:10])})
    if num_marks and not ref_entries:
        issues.append({"severity": "high", "code": "no_ref_list",
                       "message": "正文有编号引用但检测不到参考文献列表",
                       "evidence": f"{len(num_marks)} 个编号引用"})
    if not num_marks and not author_year_marks and len(text) > 2000:
        issues.append({"severity": "high", "code": "no_citations",
                       "message": "全文未检测到任何文内引用标记", "evidence": "[n] 与 (Author, year) 均为 0"})
    if metrics["figure_mentions"] == 0 and metrics["table_mentions"] == 0 and cat_share.get("实验", 0) > 0.1:
        issues.append({"severity": "low", "code": "no_figures",
                       "message": "有实验章节但全文未引用任何图表",
                       "evidence": "图 n / 表 n / Figure / Table 计数均为 0"})

    sev_order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: sev_order.get(x["severity"], 3))

    return {
        "filename": filename,
        "sections": [{k: s[k] for k in ("title", "level", "chars")} for s in sections],
        "found_categories": sorted(found),
        "missing_sections": missing,
        "metrics": metrics,
        "issues": issues[:15],
        "report": _render_report(filename, sections, missing, metrics, issues[:15], body_chars),
    }


def _render_report(filename: str, sections: list[dict], missing: list[str],
                   metrics: dict, issues: list[dict], body_chars: int) -> str:
    out = [f"## 结构体检报告{f'：{filename}' if filename else ''}", ""]
    out.append("### 章节树（字符数 / 占正文比）")
    if sections:
        for s in sections:
            share = s["chars"] / body_chars if body_chars else 0
            out.append(f"{'  ' * (s['level'] - 1)}- {s['title']} — {s['chars']} 字（{share:.0%}）")
    else:
        out.append("- （未检测到章节标题——文档可能没有分节，或标题格式非常规）")
    if missing:
        out += ["", "### 缺失章节", "、".join(f"「{c}」" for c in missing)]
    out += ["", "### 指标",
            f"- 文内引用：编号 {metrics['numeric_citations']} 处 · 作者-年份 {metrics['author_year_citations']} 处",
            f"- 参考文献条目：{metrics['reference_entries']} 条"
            + (f"（疑似未引用 {len(metrics['uncited_references'])} 条）" if metrics["uncited_references"] else ""),
            f"- 图表引用：图 {metrics['figure_mentions']} 处 · 表 {metrics['table_mentions']} 处",
            f"- 摘要长度：约 {metrics['abstract_words']} 词/字"]
    if issues:
        out += ["", "### 问题清单（按严重度）"]
        icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}
        for it in issues:
            out.append(f"- {icon.get(it['severity'], '⚪')} {it['message']}（依据：{it['evidence']}）")
    return "\n".join(out)
