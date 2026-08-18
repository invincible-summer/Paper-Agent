"""Deterministic discipline/intent routing for paper search sources."""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from tools.search.registry import SOURCE_SPECS

DISCIPLINES = {"cs", "medical", "biology", "physics", "math", "humanities", "social_science", "general"}
QUERY_INTENTS = {"literature", "preprint", "dataset", "software", "report", "thesis", "doi_lookup", "repository"}

@dataclass
class RouteHints:
    disciplines: list[str] = field(default_factory=list)
    query_intents: list[str] = field(default_factory=list)
    requested_sources: list[str] = field(default_factory=list)
    requires_preprints: bool = False
    requires_datasets: bool = False

@dataclass
class RouteDecision:
    primary: list[str]
    fallback: list[str]
    reasons: dict[str, str]

_KEYWORDS = {
    "cs": ("artificial intelligence", "machine learning", "deep learning", "large language model", "llm", "software", "algorithm", "computer", "人工智能", "机器学习", "大模型", "算法", "计算机"),
    "medical": ("clinical", "medicine", "medical", "patient", "disease", "therapy", "health", "医学", "临床", "患者", "疾病", "治疗", "健康"),
    "biology": ("biology", "genome", "protein", "cell", "bioinformatics", "生物", "基因", "蛋白", "细胞"),
    "physics": ("physics", "quantum", "particle", "astrophysics", "物理", "量子", "粒子"),
    "math": ("mathematics", "theorem", "algebra", "geometry", "数学", "定理", "代数", "几何"),
    "humanities": ("literature", "history", "philosophy", "linguistics", "文学", "历史", "哲学", "语言学"),
    "social_science": ("sociology", "economics", "education", "politics", "psychology", "社会学", "经济", "教育", "政治", "心理"),
}

_PRIMARY = {
    "cs": ["dblp", "arxiv", "crossref", "openalex"],
    "medical": ["europepmc", "pubmed", "medrxiv", "biorxiv"],
    "biology": ["europepmc", "biorxiv", "pubmed", "medrxiv"],
    "physics": ["arxiv", "openalex", "crossref"],
    "math": ["arxiv", "openalex", "crossref"],
    "humanities": ["crossref", "doaj", "hal", "openalex"],
    "social_science": ["crossref", "doaj", "hal", "openalex"],
    "general": ["openalex", "crossref", "doaj"],
}
_FALLBACK = {
    "cs": ["semantic_scholar", "datacite"],
    "medical": ["openalex", "crossref"],
    "biology": ["openalex", "crossref"],
    "physics": ["datacite", "openaire"],
    "math": ["datacite", "openaire"],
    "humanities": ["openaire", "datacite"],
    "social_science": ["openaire", "datacite"],
    "general": ["openaire", "datacite"],
}


def infer_route_hints(text: str, supplied: RouteHints | None = None) -> RouteHints:
    supplied = supplied or RouteHints()
    lower = (text or "").lower()
    disciplines = [d for d in supplied.disciplines if d in DISCIPLINES]
    if not disciplines:
        scores = {d: sum(1 for word in words if word in lower) for d, words in _KEYWORDS.items()}
        ranked = [d for d, score in sorted(scores.items(), key=lambda row: row[1], reverse=True) if score]
        disciplines = ranked[:2] or ["general"]
    intents = [i for i in supplied.query_intents if i in QUERY_INTENTS]
    if supplied.requires_preprints or re.search(r"\bpreprints?\b|预印本", lower):
        intents.append("preprint")
    if supplied.requires_datasets or re.search(r"\bdatasets?\b|数据集", lower):
        intents.append("dataset")
    inferred_intents = {
        "software": r"\bresearch software\b|\bsoftware artifacts?\b|研究软件|软件产物",
        "report": r"\btechnical reports?\b|\bresearch reports?\b|技术报告|研究报告",
        "thesis": r"\bthes(?:is|es)\b|\bdissertations?\b|学位论文|毕业论文",
        "doi_lookup": r"\bdoi\b|数字对象标识符",
        "repository": r"\binstitutional repositor(?:y|ies)\b|\bopen access repositor(?:y|ies)\b|机构库|机构仓储|开放仓储",
    }
    for intent, pattern in inferred_intents.items():
        if re.search(pattern, lower):
            intents.append(intent)
    requested = [s for s in supplied.requested_sources if s in SOURCE_SPECS]
    for source, spec in SOURCE_SPECS.items():
        if source in lower or spec.display_name.lower() in lower:
            requested.append(source)
    return RouteHints(
        disciplines=list(dict.fromkeys(disciplines)),
        query_intents=list(dict.fromkeys(intents or ["literature"])),
        requested_sources=list(dict.fromkeys(requested)),
        requires_preprints="preprint" in intents,
        requires_datasets="dataset" in intents,
    )


def choose_sources(hints: RouteHints, enabled: list[str], eligible: set[str]) -> RouteDecision:
    allowed = set(enabled) & eligible
    primary: list[str] = []
    fallback: list[str] = []
    reasons: dict[str, str] = {}

    def add(target: list[str], source: str, reason: str, cap: int) -> None:
        if source in allowed and source not in primary and source not in fallback and len(target) < cap:
            target.append(source); reasons[source] = reason

    for source in hints.requested_sources:
        add(primary, source, "用户明确点名", 4)
    disciplines = hints.disciplines or ["general"]

    intents = set(hints.query_intents)
    if "repository" in intents:
        for source in ("openaire", "hal", "doaj"):
            add(primary, source, "需要 OA/机构仓储", 4)
    object_intents = intents & {"dataset", "software", "report", "thesis"}
    if object_intents or hints.requires_datasets:
        label = "、".join(sorted(object_intents or {"dataset"}))
        for source in ("datacite", "crossref", "openalex", "hal"):
            add(primary, source, f"研究产物类型 {label}", 4)
    if "doi_lookup" in intents:
        for source in ("crossref", "datacite", "openalex"):
            add(primary, source, "DOI 精确查询", 4)
    if "preprint" in intents or hints.requires_preprints:
        if any(d in {"medical", "biology"} for d in disciplines):
            candidates = ("medrxiv", "biorxiv", "europepmc")
        elif any(d in {"cs", "physics", "math"} for d in disciplines):
            candidates = ("arxiv",)
        else:
            candidates = ("arxiv", "biorxiv", "medrxiv")
        for source in candidates:
            add(primary, source, "需要预印本", 4)

    for discipline in disciplines:
        for source in _PRIMARY.get(discipline, _PRIMARY["general"]):
            add(primary, source, f"匹配学科 {discipline}", 4)

    if "repository" in intents:
        add(fallback, "core", "OA/机构仓储兜底", 2)
    for discipline in disciplines:
        for source in _FALLBACK.get(discipline, _FALLBACK["general"]):
            add(fallback, source, f"{discipline} 兜底", 2)
    return RouteDecision(primary=primary[:4], fallback=fallback[:2], reasons=reasons)
