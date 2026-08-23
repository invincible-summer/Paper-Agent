"""Central registry for paper-search source capabilities and compliance gates."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    id: str
    display_name: str
    coverage: str
    protocol: str
    license_status: str
    routing_tags: tuple[str, ...]
    requires_key: str | None = None
    requires_license_confirmation: str | None = None
    supports_remote_search: bool = True
    supports_abstract: bool = True
    default_enabled: bool = False
    max_queries_per_turn: int = 2


SOURCE_SPECS: dict[str, SourceSpec] = {
    "openalex": SourceSpec(
        "openalex", "OpenAlex", "综合学科元数据与引用图谱", "REST API",
        "CC0 元数据；官方 API 需 Key", ("general", "cs", "medical", "physics", "humanities", "dataset"),
        requires_key="OPENALEX_API_KEY", default_enabled=True,
    ),
    "semantic_scholar": SourceSpec(
        "semantic_scholar", "Semantic Scholar", "学术图谱、引用与计算机科学", "REST API",
        "条件许可；需运营者确认", ("cs", "general"), requires_key="S2_API_KEY",
        requires_license_confirmation="S2_LICENSE_CONFIRMED", default_enabled=True,
    ),
    "arxiv": SourceSpec(
        "arxiv", "arXiv", "计算机、数学、物理等预印本", "Atom API",
        "开放元数据与摘要；全文分析需上传原文", ("cs", "physics", "math", "preprint"),
        default_enabled=True, max_queries_per_turn=2,
    ),
    "crossref": SourceSpec(
        "crossref", "Crossref", "跨学科 DOI 元数据", "REST API",
        "开放 API；提交元数据许可不一", ("general", "cs", "medical", "physics", "humanities", "dataset"),
        default_enabled=True,
    ),
    "europepmc": SourceSpec(
        "europepmc", "Europe PMC", "生命科学、医学和预印本", "REST API",
        "公开 API；摘要逐条获取，全文分析需上传原文", ("medical", "biology", "preprint"), default_enabled=True,
    ),
    "doaj": SourceSpec(
        "doaj", "DOAJ", "开放获取期刊", "REST API v4", "文章元数据 CC0",
        ("general", "humanities", "medical", "social_science"), default_enabled=True,
    ),
    "hal": SourceSpec(
        "hal", "HAL", "法国国家开放仓储、人文社科和学位论文", "Solr REST API",
        "法国开放数据许可；摘要按记录获取，全文分析需上传原文", ("humanities", "social_science", "repository", "thesis"), default_enabled=True,
    ),
    "openaire": SourceSpec(
        "openaire", "OpenAIRE", "欧盟开放研究图谱和机构仓储", "Graph API V3",
        "Graph 数据 CC-BY", ("repository", "eu", "general", "oa"), default_enabled=True,
    ),
    "core": SourceSpec(
        "core", "CORE", "开放仓储聚合", "REST API v3", "条件许可；需运营者确认",
        ("repository", "oa", "general"), requires_key="CORE_API_KEY",
        requires_license_confirmation="CORE_LICENSE_CONFIRMED", default_enabled=True,
    ),
    "biorxiv": SourceSpec(
        "biorxiv", "bioRxiv", "生命科学预印本本地元数据索引", "Official metadata API + SQLite FTS5",
        "公开 API；摘要逐条获取，全文分析需上传原文", ("biology", "medical", "preprint"),
        supports_remote_search=False,
    ),
    "medrxiv": SourceSpec(
        "medrxiv", "medRxiv", "医学预印本本地元数据索引", "Official metadata API + SQLite FTS5",
        "公开 API；摘要逐条获取，全文分析需上传原文", ("medical", "preprint"),
        supports_remote_search=False,
    ),
    "pubmed": SourceSpec(
        "pubmed", "PubMed", "NLM 权威生物医学文献索引", "NCBI E-utilities",
        "公开 API；记录/摘要可能受版权保护", ("medical", "biology"), default_enabled=False,
    ),
    "datacite": SourceSpec(
        "datacite", "DataCite", "数据集、软件、报告、学位论文和 DOI", "REST API",
        "元数据 CC0", ("dataset", "software", "report", "thesis", "general"),
        default_enabled=False,
        # DataCite's official DOI metadata schema permits Abstract descriptions,
        # even though any given record may omit one.  Diagnose this separately
        # from search rather than presenting the cell as unsupported.
        supports_abstract=True,
    ),
    "dblp": SourceSpec(
        "dblp", "DBLP", "计算机科学出版物", "Publication Search API",
        "元数据 CC0", ("cs",), default_enabled=False,
        supports_abstract=False,
    ),
}

SOURCE_IDS = tuple(SOURCE_SPECS)
NEW_SOURCE_IDS = ("biorxiv", "medrxiv", "pubmed", "datacite", "dblp")


def source_defaults(*, existing_install: bool = False) -> dict[str, bool]:
    """New sources stay disabled during an upgrade; fresh seeds use registry defaults."""
    return {
        name: (False if existing_install and name in NEW_SOURCE_IDS else spec.default_enabled)
        for name, spec in SOURCE_SPECS.items()
    }


def contact_email(settings=None) -> str:
    if settings is None:
        from core.config import get_settings
        settings = get_settings().search
    return (getattr(settings, "paper_platform_contact_email", "") or getattr(settings, "crossref_email", "")
            or getattr(settings, "openalex_email", "") or "").strip()


def runtime_gate(source: str, settings=None) -> tuple[bool, str]:
    if settings is None:
        from core.config import get_settings
        settings = get_settings().search
    if source == "openalex" and not getattr(settings, "openalex_api_key", ""):
        return False, "missing_api_key"
    if source == "semantic_scholar":
        if not getattr(settings, "s2_api_key", ""):
            return False, "missing_api_key"
        if not getattr(settings, "s2_license_confirmed", False):
            return False, "license_not_confirmed"
    if source == "core":
        if not getattr(settings, "core_api_key", ""):
            return False, "missing_api_key"
        if not getattr(settings, "core_license_confirmed", False):
            return False, "license_not_confirmed"
    if source == "pubmed" and not contact_email(settings):
        return False, "missing_contact_email"
    if source in {"biorxiv", "medrxiv"}:
        from tools.search.rxiv_catalog import sync_status
        try:
            indexed_count = int(sync_status(source).get("indexed_count") or 0)
        except Exception:
            return False, "index_unavailable"
        if indexed_count <= 0:
            return False, "index_empty"
    return True, "ready"
