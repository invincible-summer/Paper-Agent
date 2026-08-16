"""structure_check: deterministic draft structure analysis."""
from tools.writing.structure_check import analyze_draft

MD_DRAFT = """# My Paper Title

## Abstract
This paper proposes a method. It works well.

## 1. Introduction
Graph neural networks are important [1]. Many works try to improve them [2].
This section motivates the problem and states the contributions of the paper.
""" + "Padding introduction text. " * 40 + """

## 2. Method
We present our model. """ + "Method details. " * 30 + """

## 3. Experiments
We evaluate on datasets. """ + "Experiment results. " * 60 + """

## 4. Conclusion
We conclude.

## References
[1] Smith et al. Graph nets. 2020.
[2] Lee et al. More graph nets. 2021.
[3] Wang et al. Never cited work. 2019.
"""

ZH_DRAFT = """一、引言
""" + "中文引言正文。" * 60 + """
二、方法
""" + "中文方法正文。" * 50 + """
三、实验
""" + "中文实验正文。" * 80 + """
四、结论
中文结论正文。
参考文献
[1] 张三. 某论文. 2020.
"""


def test_markdown_sections_parsed():
    r = analyze_draft(MD_DRAFT, filename="draft.md")
    titles = [s["title"] for s in r["sections"]]
    assert "Abstract" in titles and "1. Introduction" in titles
    body_sections = [s for s in r["sections"] if s["title"] not in ("My Paper Title", "4. Conclusion")]
    assert all(s["chars"] > 0 for s in body_sections)


def test_missing_sections_flagged():
    r = analyze_draft(MD_DRAFT)
    # draft has no related-work / discussion section
    assert "相关工作" in r["missing_sections"]
    assert "讨论" in r["missing_sections"]
    assert "方法" not in r["missing_sections"]
    missing_codes = [i["code"] for i in r["issues"]]
    assert "missing_section" in missing_codes


def test_uncited_references_detected():
    r = analyze_draft(MD_DRAFT)
    assert r["metrics"]["reference_entries"] == 3
    assert r["metrics"]["numeric_citations"] == 2
    assert r["metrics"]["uncited_references"] == [3]
    assert any(i["code"] == "uncited_refs" for i in r["issues"])


def test_short_abstract_flagged():
    r = analyze_draft(MD_DRAFT)
    assert r["metrics"]["abstract_words"] < 80
    assert any(i["code"] == "abstract_short" for i in r["issues"])


def test_chinese_headings_parsed():
    r = analyze_draft(ZH_DRAFT)
    found = r["found_categories"]
    for cat in ("引言", "方法", "实验", "结论", "参考文献"):
        assert cat in found, cat
    assert "摘要" in r["missing_sections"]


def test_no_citations_flagged_on_long_text():
    text = "一、引言\n" + "没有任何引用的正文。" * 300 + "\n二、结论\n完。"
    r = analyze_draft(text)
    assert any(i["code"] == "no_citations" for i in r["issues"])


def test_author_year_citations_counted():
    text = ("## 1. Introduction\nPrior work (Smith et al., 2020) and Lee (2021) showed things. "
            + "More text. " * 80 +
            "\n## 2. Conclusion\nDone.\n## References\nSmith. 2020.\nLee. 2021.\n")
    r = analyze_draft(text)
    assert r["metrics"]["author_year_citations"] >= 2


def test_report_renders_markdown():
    r = analyze_draft(MD_DRAFT, filename="draft.md")
    assert r["report"].startswith("## 结构体检报告：draft.md")
    assert "章节树" in r["report"] and "问题清单" in r["report"]
