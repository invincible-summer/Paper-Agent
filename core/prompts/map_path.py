"""Prompts for research map (clusters / landscape) and reading path."""
from __future__ import annotations

from core.prompts.registry import get, register

_CLUSTER_LABELS = """以下是同一研究领域下按语义聚出的若干论文簇（每簇含若干论文标题）。

{clusters_text}

为每个簇生成：
- label：简短的簇名称（中文 ≤12 字 / English ≤6 words），要能体现该簇的共同主题；
- overview：1-2 句概述，说明这些论文共同在研究什么、方法或视角有何共性。
{language_instruction}

只输出一个 JSON 对象，不要任何其它文字或 markdown 代码块：
{{"clusters": [{{"id": "簇id", "label": "...", "overview": "..."}}]}}"""

_LANDSCAPE = """基于以下各主题簇的名称与概述，用 3-5 句话勾勒这个研究领域的整体脉络：哪些方向是奠基性的，哪些是当前前沿，方向之间如何演进、分化或交叉。{language_instruction}

{clusters_text}

只输出连贯段落，不要使用列表或标题。"""

_READING_PATH = """以下是为入门该研究领域挑选的论文，按推荐阅读顺序排列，每篇带角色标签（奠基 / 桥梁 / 前沿）。

{papers_text}

为每篇写一句推荐理由（≤40 字 / ≤25 words）：为什么先读它、重点读什么。理由要具体，结合该论文的主题，不要写"这是经典论文"之类的空话。{language_instruction}

只输出一个 JSON 对象，不要任何其它文字或 markdown 代码块：
{{"reasons": [{{"paper_id": "...", "reason": "..."}}]}}"""

register("map.cluster_labels", 1, _CLUSTER_LABELS)
register("map.landscape", 1, _LANDSCAPE)
register("path.reading_reasons", 1, _READING_PATH)


def get_cluster_labels_prompt() -> str:
    return get("map.cluster_labels").text


def get_landscape_prompt() -> str:
    return get("map.landscape").text


def get_reading_path_prompt() -> str:
    return get("path.reading_reasons").text

_MAP_SUMMARY = """以下是同一研究领域按语义或词法重叠形成的论文簇，每篇论文标注了年份与被引数。\n\n{clusters_text}\n\n请一次完成：\n1. 为每个簇生成简短 label（中文不超过12字 / English no more than 6 words）与 1-2 句 overview（说明该簇在研究什么、方法或视角有何共性）；\n2. 生成 landscape：按固定格式逐簇规范化描述领域脉络——第一句概括该领域整体定位与年份跨度；随后每个簇单独一行，格式为「【簇label】（N 篇，起始年–结束年）：overview 要点；代表论文：《标题》（年份）。」代表论文从该簇被引数最高的 1-3 篇中选取；最后 1-2 句说明簇之间的演进、分化或交叉关系。\nlandscape 中只能使用上方输入里真实出现的论文标题、年份与数字，禁止编造或改写标题。\n{language_instruction}\n\n只输出 JSON 对象，不要 markdown：\n{{\"clusters\":[{{\"id\":\"0\",\"label\":\"...\",\"overview\":\"...\"}}],\"landscape\":\"...\"}}"""
register("map.summary", 3, _MAP_SUMMARY)


def get_map_summary_prompt() -> str:
    return get("map.summary").text
