"""Vision prompts for paper-element understanding.

Registered in the prompt registry (ID + version) per AGENTS.md. prompt_version
is part of the vision_cache key, so bumping a version cleanly invalidates the
cached analyses for that task without re-running anything else.
"""

from __future__ import annotations

from core.prompts.registry import register

# --- figure --------------------------------------------------------------------
_FIGURE_PROMPT = """你是学术论文图表分析专家。分析给定的论文图片，仅返回一个 JSON 对象（不要 markdown 代码块、不要任何解释文字）。
字段：
- type: 图片类型，取值之一 architecture(模型/系统结构图) | result_curve(实验曲线/指标图) | visualization(可视化/定性结果) | flowchart(流程/算法图) | diagram(示意图) | table_screenshot(表格截图) | other
- description: 中文，2-4 句，客观描述图片主体内容
- components: 主要组成模块/对象/曲线的名称列表（字符串数组）
- relations: 组件之间的关系列表（字符串数组，例如 "encoder 输出喂入 attention 模块"）
- role_in_paper: 该图在论文中的作用，1 句话
若某字段无法判断，给空字符串或空数组。仅返回 JSON 对象。"""

# --- table ---------------------------------------------------------------------
_TABLE_PROMPT = """你是学术论文表格分析专家。分析给定的论文表格图片，仅返回一个 JSON 对象（不要 markdown 代码块）。
字段：
- markdown: 该表格对应的 Markdown 表格（尽力还原行列结构与数值，列名准确）
- summary: 中文，1-3 句，概括这张表说明了什么
- key_metrics: 表中最关键的指标/数值列表（字符串数组，例如 "准确率 92.3%"）
- comparison_axes: 表格比较的维度列表（字符串数组，例如 "方法 / 数据集 / 准确率"）
若某字段无法判断，给空字符串或空数组。仅返回 JSON 对象。"""

# --- formula -------------------------------------------------------------------
_FORMULA_PROMPT = """你是学术论文公式分析专家。分析给定的论文公式图片，仅返回一个 JSON 对象（不要 markdown 代码块）。
字段：
- latex: 该公式对应的 LaTeX 源码（仅公式本体，不要 $ 定界符，可包含 \\frac \\sum \\int 等）
- meaning: 中文，1-2 句，解释这个公式的数学含义
- variables: 公式中主要变量到含义的映射（JSON 对象，键为变量符号，值为中文含义）
- role: 该公式在论文方法中的作用，1 句话（例如 "训练目标 / 交叉熵损失"）
若无法识别为公式，latex 给空字符串并在 meaning 注明 "未识别为公式"。仅返回 JSON 对象。"""

# --- ocr (scanned-page fallback) ----------------------------------------------
_OCR_PROMPT = """这是学术论文某一页的扫描图。请进行 OCR 转写：还原该页正文文本，保持段落与阅读顺序，公式尽量用 LaTeX（行内用 $...$），表格用 Markdown。只输出转写后的正文，不要解释、不要前后缀。若有图表区域无法用文字表达，用 [Figure]/[Table] 占位。"""


def register_vision_prompts() -> None:
    """Register all vision prompts. Idempotent (re-register overwrites in place)."""
    register("vision.figure", 1, _FIGURE_PROMPT)
    register("vision.table", 1, _TABLE_PROMPT)
    register("vision.formula", 1, _FORMULA_PROMPT)
    register("vision.ocr", 1, _OCR_PROMPT)


# Register at import so a prompt-version snapshot is available to the trace
# from the first turn, the same way system.py / search.py register theirs.
register_vision_prompts()
