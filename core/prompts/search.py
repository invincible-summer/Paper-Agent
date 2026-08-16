"""Search-stage prompts: intent understanding + query decomposition."""
from __future__ import annotations

from core.prompts.registry import get, register

_UNDERSTAND = """你是学术检索规划器。根据用户的研究主题与补充说明，产出一份检索规划。

研究主题: {topic}
补充说明: {conception}
语言偏好: {language_instruction}

要求：
1. 若主题过短或是缩写（如 NLP、GNN），先还原为最可能的学术全称，写进 research_goal。
2. 把主题拆解为 2-4 个具体研究方向（sub_directions），方向之间应有区分度。
3. 为每个方向给 1-2 个英文检索式（学术关键词风格，2-6 个词，适合 OpenAlex/arXiv 检索）；语言偏好不是纯英文时，每个方向再给 1 个中文检索式（2-4 个词）。
4. 只输出一个 JSON 对象，不要任何其它文字或 markdown 代码块：
{{"research_goal": "还原后的完整研究目标", "sub_directions": [{{"name": "方向名", "queries_en": ["..."], "queries_zh": ["..."]}}]}}"""

register("search.understand", 1, _UNDERSTAND)


def get_understand_prompt() -> str:
    return get("search.understand").text
