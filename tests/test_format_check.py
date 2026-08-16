"""format_check: deterministic manuscript format rules."""
from tools.writing.format_check import analyze_format

TEXT_DRAFT = """# 论文标题

## 摘要
""" + "这是一个测试摘要。" * 30 + """
关键词：图神经网络；推荐系统

## 1 引言
""" + "引言内容。" * 100 + """ 如图 1 所示，表 1 给出结果，图 3 是对比。

## 2 方法
""" + "方法内容。" * 100 + """

## 2.1 模型
模型细节。
## 2.3 训练
训练细节。

## 3 结论
结论。

## 参考文献
[1] Smith J. Some title. Nature, 2020.
[2] 张三. 某某研究[J]. 计算机学报, 2021, 44(1): 1-10.
"""

LATEX_DOC = r"""\documentclass{article}
\begin{document}
\begin{abstract}
This is an abstract with enough words to pass the minimum threshold for testing.
\end{abstract}
\section{Introduction}
We cite \cite{smith2020} and show \ref{fig:model} plus \ref{fig:ghost}.
\begin{figure}\label{fig:model}\end{figure}
\subsection{Method}
Text.
\end{document}
"""


def test_figure_number_gap_detected():
    r = analyze_format(TEXT_DRAFT)
    assert any(i["code"] == "图_gap" for i in r["issues"])  # 图 2 missing


def test_heading_number_gap_detected():
    r = analyze_format(TEXT_DRAFT)
    assert any(i["code"] == "heading_gap" and "2.2" in i["message"] for i in r["issues"])


def test_gbt7714_hit_rate_measured():
    r = analyze_format(TEXT_DRAFT)
    # one of two ref lines has [J] -> rate 0.5, below 0.6 threshold is not an issue at >=0.5
    assert r["metrics"]["gbt7714_hit_rate"] == 0.5


def test_keywords_extracted():
    r = analyze_format(TEXT_DRAFT)
    assert r["metrics"]["keywords"] == ["图神经网络", "推荐系统"]


def test_spec_abstract_and_keywords():
    spec = "摘要不超过 300 字；关键词 3-5 个；参考文献按 GB/T 7714"
    r = analyze_format(TEXT_DRAFT, spec=spec)
    by_kind = {s["requirement"]: s for s in r["spec_results"]}
    assert len(r["spec_results"]) == 3
    kw = [s for s in r["spec_results"] if "关键词" in s["requirement"]][0]
    assert kw["ok"] is False  # only 2 keywords, requires 3-5
    gbt = [s for s in r["spec_results"] if "GB/T" in s["requirement"]][0]
    assert gbt["ok"] is False  # hit rate 0.5 < 0.6


def test_spec_layout_goes_to_manual():
    spec = "正文用宋体小四号字，1.5 倍行距；摘要不超过 300 字"
    r = analyze_format(TEXT_DRAFT, spec=spec)
    assert any("宋体" in m or "行距" in m for m in r["layout_manual_checks"])
    assert "宋体" in r["report"]


def test_latex_ref_without_label_flagged():
    r = analyze_format(LATEX_DOC)
    assert r["is_latex"] is True
    assert any(i["code"] == "latex_ref_missing" and "fig:ghost" in i["evidence"]
               for i in r["issues"])


def test_latex_cite_without_bibliography_flagged():
    r = analyze_format(LATEX_DOC)
    assert any(i["code"] == "latex_no_bib" for i in r["issues"])


def test_citation_style_mixing_flagged():
    text = ("## 1 引言\n" + "正文。" * 100 + "有编号引用 [1]，也有作者年份引用 (Smith et al., 2020)。\n"
            "## 参考文献\n[1] x. 2020.\n")
    r = analyze_format(text)
    assert any(i["code"] == "cite_style_mixed" for i in r["issues"])


def test_report_renders():
    r = analyze_format(TEXT_DRAFT, filename="paper.md", spec="摘要不超过 300 字")
    assert r["report"].startswith("## 格式检查报告：paper.md")
