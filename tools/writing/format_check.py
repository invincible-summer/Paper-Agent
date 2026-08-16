"""Deterministic manuscript format checker (论文格式检查) — pure Python.

Complements structure_check (宏观结构) with 微观格式规则. Two modes picked
by content: LaTeX sources (\\section, environments) and extracted plain
text (pdf/docx/txt/md). A user-supplied format spec (`spec`, free text
pasted from the venue/school requirement) is parsed into the text-checkable
subset; pure typesetting requirements (fonts/margins/spacing) are honestly
routed to a "manual check in Word/LaTeX" list instead of fake-verified.
"""
from __future__ import annotations

import re

from tools.writing.structure_check import analyze_draft

_FIG_TXT = re.compile(r"图\s*(\d+)|figure\s*(\d+)|fig\.\s*(\d+)", re.I)
_TAB_TXT = re.compile(r"表\s*(\d+)|table\s*(\d+)", re.I)
_NUM_CITE = re.compile(r"\[(\d+(?:\s*[-–,]\s*\d+)*)\]")
_AUTHOR_YEAR = re.compile(r"\([A-Z][A-Za-z\-]+(?:\s+et\s+al\.?|\s+and\s+[A-Z][A-Za-z\-]+)?,?\s+\d{4}[a-z]?\)")
_LATEX_CITE = re.compile(r"\\cite\w*\{[^}]*\}")
_LATEX_LABEL = re.compile(r"\\label\{((?:fig|tab|figure|table)[^}]*)\}", re.I)
_LATEX_REF = re.compile(r"\\(?:ref|eqref|autoref)\{([^}]*)\}")
_HEAD_NUM = re.compile(r"^(?P<num>\d+(?:\.\d+)*)\s*[.、\s]\s*\S")
_KEYWORDS = re.compile(r"(?:关键词|关键字|key\s*words?)\s*[:：]\s*(.+)", re.I)
_GBT_MARK = re.compile(r"\[(J|M|C|D|R|P|S|N|EB|OL|A)\]")
_REF_LINE = re.compile(r"^\s*(?:\[\d+\]|\d{1,2}[.、\)])")
_LATEX_BIB = re.compile(r"\\begin\{thebibliography\}|\\bibliography\{|\\addbibresource", re.I)
_LAYOUT_WORDS = re.compile(
    r"字体|字号|宋体|黑体|楷体|Times|行距|行间距|页边距|页眉|页脚|磅|pt\b|居中|缩进", re.I)


def _numbered_heading_gaps(lines: list[str]) -> list[str]:
    """Missing sibling numbers: 1.1 followed by 1.3 (no 1.2)."""
    seen: dict[str, set[int]] = {}
    order: list[tuple[str, int]] = []
    for line in lines:
        text = line.strip().lstrip("#").strip()  # allow "## 2.1 标题" too
        m = _HEAD_NUM.match(text)
        if not m or len(line) > 90:
            continue
        parts = m.group("num").split(".")
        if len(parts) < 2:
            continue
        parent, leaf = ".".join(parts[:-1]), int(parts[-1])
        seen.setdefault(parent, set()).add(leaf)
        order.append((parent, leaf))
    gaps: list[str] = []
    for parent, leafs in seen.items():
        if not leafs:
            continue
        for n in range(min(leafs), max(leafs) + 1):
            if n not in leafs:
                gaps.append(f"{parent}.{n}")
    return gaps[:10]


def _spec_rules(spec: str) -> list[dict]:
    """Parse a free-text format spec into executable checks + manual items."""
    checks: list[dict] = []
    for sent in re.split(r"[。；;\n]", spec or ""):
        s = sent.strip()
        if not s:
            continue
        if _LAYOUT_WORDS.search(s):
            checks.append({"kind": "manual", "requirement": s})
            continue
        m = re.search(r"摘要[^0-9]{0,12}(?:不超过|不大于|以内|之内|≤|控制在)\s*(\d{2,4})\s*字", s)
        if m:
            checks.append({"kind": "abstract_max", "limit": int(m.group(1)),
                           "requirement": s})
            continue
        m = re.search(r"(?:正文|全文)[^0-9]{0,12}(?:不少于|不低于|至少|≥)\s*(\d{3,6})\s*字", s)
        if m:
            checks.append({"kind": "body_min", "limit": int(m.group(1)),
                           "requirement": s})
            continue
        m = re.search(r"关键词[^0-9]{0,8}(\d)\s*(?:[-~～到至]\s*(\d))?\s*个", s)
        if m:
            lo = int(m.group(1))
            hi = int(m.group(2)) if m.group(2) else lo
            checks.append({"kind": "keywords", "lo": lo, "hi": hi, "requirement": s})
            continue
        if re.search(r"GB/?T?\s*7714|国标", s, re.I):
            checks.append({"kind": "gbt7714", "requirement": s})
            continue
        checks.append({"kind": "manual", "requirement": s})
    return checks


def analyze_format(text: str, filename: str = "", spec: str = "") -> dict:
    """Format-check one manuscript's text. Returns facts + markdown report."""
    base = analyze_draft(text, filename=filename)  # reuse structure parsing
    lines = text.splitlines()
    is_latex = bool(re.search(r"\\(section|subsection|begin\{document\})", text))
    issues: list[dict] = []

    # --- figures / tables ---
    if is_latex:
        labels = set(_LATEX_LABEL.findall(text))
        refs = set(_LATEX_REF.findall(text))
        missing = sorted(r for r in refs if r not in labels and not r.startswith("eq"))
        unreferenced = sorted(l for l in labels if l not in refs)
        if missing:
            issues.append({"severity": "high", "code": "latex_ref_missing",
                           "message": f"{len(missing)} 个 \\ref 没有对应的 \\label",
                           "evidence": "、".join(missing[:6])})
        if unreferenced:
            issues.append({"severity": "low", "code": "latex_label_unref",
                           "message": f"{len(unreferenced)} 个图表 \\label 从未在正文 \\ref 引用",
                           "evidence": "、".join(unreferenced[:6])})
    else:
        fig_nums = sorted({int(a or b or c) for a, b, c in _FIG_TXT.findall(text)})
        tab_nums = sorted({int(a or b) for a, b in _TAB_TXT.findall(text)})
        for label, nums in (("图", fig_nums), ("表", tab_nums)):
            if nums:
                missing = [n for n in range(nums[0], nums[-1] + 1) if n not in nums]
                if missing:
                    issues.append({"severity": "medium", "code": f"{label}_gap",
                                   "message": f"{label}编号不连续（缺 {'、'.join(map(str, missing[:8]))}）",
                                   "evidence": f"出现的编号：{nums[:12]}"})

    # --- citation style ---
    n_numeric = len(_NUM_CITE.findall(text))
    n_authoryear = len(_AUTHOR_YEAR.findall(text))
    n_latex_cite = len(_LATEX_CITE.findall(text))
    if is_latex:
        if n_latex_cite == 0 and (n_numeric or n_authoryear):
            issues.append({"severity": "medium", "code": "latex_manual_cite",
                           "message": "LaTeX 文稿中未使用 \\cite，而是手写引用标记",
                           "evidence": "建议改用 \\cite + 参考文献管理"})
        if n_latex_cite > 0 and not _LATEX_BIB.search(text):
            issues.append({"severity": "high", "code": "latex_no_bib",
                           "message": "使用了 \\cite 但找不到参考文献块",
                           "evidence": "缺 thebibliography 环境或 \\bibliography/\\addbibresource"})
    elif n_numeric and n_authoryear:
        issues.append({"severity": "medium", "code": "cite_style_mixed",
                       "message": "数字编号引用与作者-年份引用混用",
                       "evidence": f"[n] {n_numeric} 处、(Author, year) {n_authoryear} 处，应统一为一种"})

    # --- GB/T 7714 hit rate (text reference list) ---
    ref_lines = [l for l in lines if _REF_LINE.match(l)]
    gbt_hits = sum(1 for l in ref_lines if _GBT_MARK.search(l))
    gbt_rate = gbt_hits / len(ref_lines) if ref_lines else None
    if ref_lines and gbt_rate is not None and gbt_rate < 0.5:
        issues.append({"severity": "medium", "code": "gbt7714_low",
                       "message": f"参考文献 GB/T 7714 特征命中率仅 {gbt_rate:.0%}",
                       "evidence": f"{len(ref_lines)} 条中 {gbt_hits} 条含 [J]/[M] 等类型标识"})

    # --- keywords ---
    kw_line = next((m.group(1) for m in (_KEYWORDS.search(l) for l in lines) if m), None)
    kw_list = [k for k in re.split(r"[,，;；、]", kw_line or "") if k.strip()]
    if kw_line is None and base["metrics"]["total_chars"] > 3000:
        issues.append({"severity": "medium", "code": "no_keywords",
                       "message": "未检测到关键词行", "evidence": "缺少 关键词:/Keywords: 段"})
    elif kw_list and not (3 <= len(kw_list) <= 8):
        issues.append({"severity": "low", "code": "keywords_count",
                       "message": f"关键词 {len(kw_list)} 个（常规 3-8 个）",
                       "evidence": "、".join(k.strip() for k in kw_list[:10])})

    # --- numbered-heading gaps (plain text only) ---
    if not is_latex:
        gaps = _numbered_heading_gaps(lines)
        if gaps:
            issues.append({"severity": "low", "code": "heading_gap",
                           "message": f"标题编号疑似断号：缺 {'、'.join(gaps[:6])}",
                           "evidence": "多级编号连续性检查"})

    # --- user spec ---
    spec_results: list[dict] = []
    manual_checks: list[str] = []
    for rule in _spec_rules(spec):
        kind = rule["kind"]
        req = rule["requirement"]
        if kind == "manual":
            manual_checks.append(req)
        elif kind == "abstract_max":
            words = base["metrics"]["abstract_words"]
            ok_ = 0 < words <= rule["limit"] * 1.1
            spec_results.append({"requirement": req, "ok": ok_,
                                 "detail": f"实测约 {words} 词/字（上限 {rule['limit']}）"})
            if not ok_:
                issues.append({"severity": "medium", "code": "spec_abstract",
                               "message": f"摘要长度不符合要求：约 {words} / 上限 {rule['limit']} 字",
                               "evidence": req})
        elif kind == "body_min":
            cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
            ok_ = cjk >= rule["limit"]
            spec_results.append({"requirement": req, "ok": ok_,
                                 "detail": f"实测中文字符约 {cjk}（下限 {rule['limit']}）"})
            if not ok_:
                issues.append({"severity": "medium", "code": "spec_body",
                               "message": f"正文字数不达标：约 {cjk} / 要求 ≥{rule['limit']}",
                               "evidence": req})
        elif kind == "keywords":
            n = len(kw_list)
            ok_ = kw_list != [] and rule["lo"] <= n <= rule["hi"]
            spec_results.append({"requirement": req, "ok": ok_,
                                 "detail": f"实测 {n} 个（要求 {rule['lo']}-{rule['hi']}）"})
            if not ok_:
                issues.append({"severity": "low", "code": "spec_keywords",
                               "message": f"关键词数量不符：{n} / 要求 {rule['lo']}-{rule['hi']} 个",
                               "evidence": req})
        elif kind == "gbt7714":
            ok_ = gbt_rate is not None and gbt_rate >= 0.6
            spec_results.append({"requirement": req, "ok": ok_,
                                 "detail": ("类型标识命中率 "
                                            f"{gbt_rate:.0%}" if gbt_rate is not None
                                            else "未检测到参考文献列表")})
            if not ok_:
                issues.append({"severity": "medium", "code": "spec_gbt",
                               "message": "参考文献不满足 GB/T 7714 要求", "evidence": req})

    sev_order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: sev_order.get(x["severity"], 3))

    return {
        "filename": filename,
        "is_latex": is_latex,
        "metrics": {
            "figures_referenced": len(set(_FIG_TXT.findall(text))) if not is_latex else len(_LATEX_LABEL.findall(text)),
            "numeric_citations": n_numeric,
            "author_year_citations": n_authoryear,
            "latex_cites": n_latex_cite,
            "reference_lines": len(ref_lines),
            "gbt7714_hit_rate": round(gbt_rate, 3) if gbt_rate is not None else None,
            "keywords": [k.strip() for k in kw_list],
        },
        "issues": issues[:15],
        "spec_results": spec_results,
        "layout_manual_checks": manual_checks,
        "report": _render(filename, is_latex, issues[:15], spec_results, manual_checks),
    }


def _render(filename: str, is_latex: bool, issues: list[dict],
            spec_results: list[dict], manual: list[str]) -> str:
    out = [f"## 格式检查报告{f'：{filename}' if filename else ''}"
           f"（{'LaTeX' if is_latex else '文本'}模式）", ""]
    if issues:
        icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}
        out.append("### 问题清单（按严重度）")
        out += [f"- {icon.get(i['severity'], '⚪')} {i['message']}（依据：{i['evidence']}）"
                for i in issues]
    else:
        out.append("未检测到格式问题。")
    if spec_results:
        out += ["", "### 格式要求对照"]
        out += [f"- {'✅' if r['ok'] else '❌'} {r['requirement']} — {r['detail']}"
                for r in spec_results]
    if manual:
        out += ["", "### 需在 Word / LaTeX 中人工核对的排版项（文本提取无法判断）"]
        out += [f"- {m}" for m in manual]
    return "\n".join(out)
