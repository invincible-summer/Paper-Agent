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
3. 为每个方向给 1-2 个英文检索式（2-6 个词）；非纯英文偏好时再给 1 个中文检索式。
4. disciplines 只能从 cs/medical/biology/physics/math/humanities/social_science/general 选择。
5. query_intents 只能从 literature/preprint/dataset/software/report/thesis/doi_lookup/repository 选择。
6. requested_sources 只在用户明确点名平台时填写，可用值：openalex/semantic_scholar/arxiv/crossref/europepmc/doaj/hal/openaire/core/biorxiv/medrxiv/pubmed/datacite/dblp。
7. 只输出 JSON：
{{"research_goal":"...","disciplines":["general"],"query_intents":["literature"],"requested_sources":[],"requires_preprints":false,"requires_datasets":false,"sub_directions":[{{"name":"...","queries_en":["..."],"queries_zh":["..."]}}]}}"""

register("search.understand", 2, _UNDERSTAND)


def get_understand_prompt() -> str:
    return get("search.understand").text
