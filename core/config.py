"""Load configuration from settings.yaml and .env."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SETTINGS_PATH = _PROJECT_ROOT / "config" / "settings.yaml"


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model_light: str = "deepseek-v4-flash"
    model_reasoning: str = "deepseek-v4-flash"  # pro 暂不使用，统一 flash
    temperature: float = 0.3
    max_retries: int = 3
    timeout: int = 60


@dataclass
class MultimodalConfig:
    """OpenAI-compatible multimodal (vision) endpoint for paper-element
    understanding (figures / tables / formulas) and scanned-page OCR.

    Deliberately separate from the text LLM (DEEPSEEK_*): the text pipeline is
    untouched, and this client is used only by core/multimodal/ for vision
    tasks. Reads MULTIMODAL_* env vars; empty api_key/base_url/model disables
    vision and callers degrade to text-only (same behavior as before this
    layer existed).
    """
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    temperature: float = 0.2
    max_retries: int = 3
    timeout: int = 120
    max_concurrent: int = 2


@dataclass
class SearchConfig:
    results_per_source: int = 20
    dedup_similarity_threshold: float = 0.95
    s2_api_key: str = ""
    s2_license_confirmed: bool = False
    core_api_key: str = ""
    core_license_confirmed: bool = False
    openalex_api_key: str = ""
    paper_platform_contact_email: str = ""
    openalex_email: str = ""  # legacy contact fallback
    crossref_email: str = ""  # legacy contact fallback
    ncbi_api_key: str = ""
    openaire_client_id: str = ""
    openaire_client_secret: str = ""
    search_deadline_seconds: float = 30.0            # 多源检索总时限，超时返回部分结果
    per_source_timeout_seconds: float = 12.0         # 单条渠道查询时限
    verify_fulltext: bool = True                     # search_papers 探测 OA PDF 文件头
    fulltext_verify_timeout_seconds: float = 30.0    # 全文探测总时限（超时余下=unknown）
    force_fulltext_probe: bool = True                # 时间不足时仍为探测预留预算（管理员可关）
    fulltext_status_ttl_days: int = 30               # 已验证状态的可复用天数
    sources: dict = field(default_factory=lambda: {
        "openalex": True,
        "semantic_scholar": True,
        "arxiv": True,
        "crossref": True,        # D-083
        "europepmc": True,       # D-083
        "doaj": True,            # D-083
        "hal": True,             # OA repository, humanities/SS strong
        "openaire": True,        # EU OA graph
        "core": True,
        "biorxiv": False,
        "medrxiv": False,
        "pubmed": False,
        "datacite": False,
        "dblp": False,
    })


@dataclass
class ReaderConfig:
    pdf_dir: str = "data/pdfs"
    structure_backend: str = "docling"  # "docling" (primary) | "pymupdf" (fallback)
    assets_dir: str = "data/assets"     # extracted figure/table/formula images
    read_chunk_chars: int = 12000


@dataclass
class StorageConfig:
    sqlite_path: str = "data/metadata.db"
    chroma_dir: str = "data/chroma"
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"  # local bilingual embedder
    reranker_model: str = "BAAI/bge-reranker-base"  # cross-encoder rerank ("" disables)


@dataclass
class PerformanceConfig:
    startup_prewarm_mode: str = "blocking"
    map_citation_mode: str = "fast"


@dataclass
class Settings:
    llm: LLMConfig = field(default_factory=LLMConfig)
    multimodal: MultimodalConfig = field(default_factory=MultimodalConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)


def load_settings() -> Settings:
    """Load settings from settings.yaml, then override with env vars."""
    raw = {}
    if _SETTINGS_PATH.exists():
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    s = Settings()

    performance_raw = raw.get("performance", {})
    s.performance = PerformanceConfig(
        startup_prewarm_mode=os.getenv("STARTUP_PREWARM_MODE", performance_raw.get("startup_prewarm_mode", "blocking")),
        map_citation_mode=os.getenv("MAP_CITATION_MODE", performance_raw.get("map_citation_mode", "fast")),
    )

    llm_raw = raw.get("llm", {})
    s.llm = LLMConfig(
        api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        base_url=os.getenv("DEEPSEEK_BASE_URL", llm_raw.get("base_url", s.llm.base_url)),
        model_light=os.getenv("DEEPSEEK_MODEL_LIGHT", llm_raw.get("model_light", s.llm.model_light)),
        model_reasoning=os.getenv("DEEPSEEK_MODEL_REASONING", llm_raw.get("model_reasoning", s.llm.model_reasoning)),
        temperature=llm_raw.get("temperature", s.llm.temperature),
        max_retries=llm_raw.get("max_retries", s.llm.max_retries),
        timeout=llm_raw.get("timeout", s.llm.timeout),
    )

    multimodal_raw = raw.get("multimodal", {})
    s.multimodal = MultimodalConfig(
        api_key=os.getenv("MULTIMODAL_API_KEY", ""),
        base_url=os.getenv("MULTIMODAL_BASE_URL", multimodal_raw.get("base_url", s.multimodal.base_url)),
        model=os.getenv("MULTIMODAL_MODEL", multimodal_raw.get("model", s.multimodal.model)),
        temperature=multimodal_raw.get("temperature", s.multimodal.temperature),
        max_retries=multimodal_raw.get("max_retries", s.multimodal.max_retries),
        timeout=multimodal_raw.get("timeout", s.multimodal.timeout),
        max_concurrent=multimodal_raw.get("max_concurrent", s.multimodal.max_concurrent),
    )

    search_raw = raw.get("search", {})
    s.search = SearchConfig(
        results_per_source=search_raw.get("results_per_source", s.search.results_per_source),
        dedup_similarity_threshold=search_raw.get("dedup_similarity_threshold", s.search.dedup_similarity_threshold),
        s2_api_key=os.getenv("S2_API_KEY", ""),
        s2_license_confirmed=os.getenv("S2_LICENSE_CONFIRMED", "").lower() in {"1", "true", "yes", "on"},
        core_api_key=os.getenv("CORE_API_KEY", ""),
        core_license_confirmed=os.getenv("CORE_LICENSE_CONFIRMED", "").lower() in {"1", "true", "yes", "on"},
        openalex_api_key=os.getenv("OPENALEX_API_KEY", ""),
        paper_platform_contact_email=os.getenv("PAPER_PLATFORM_CONTACT_EMAIL", ""),
        openalex_email=os.getenv("OPENALEX_EMAIL", ""),
        crossref_email=os.getenv("CROSSREF_EMAIL", ""),
        ncbi_api_key=os.getenv("NCBI_API_KEY", ""),
        openaire_client_id=os.getenv("OPENAIRE_CLIENT_ID", ""),
        openaire_client_secret=os.getenv("OPENAIRE_CLIENT_SECRET", ""),
        search_deadline_seconds=search_raw.get(
            "search_deadline_seconds", s.search.search_deadline_seconds),
        per_source_timeout_seconds=search_raw.get(
            "per_source_timeout_seconds", s.search.per_source_timeout_seconds),
        verify_fulltext=search_raw.get("verify_fulltext", s.search.verify_fulltext),
        fulltext_verify_timeout_seconds=search_raw.get(
            "fulltext_verify_timeout_seconds", s.search.fulltext_verify_timeout_seconds),
        fulltext_status_ttl_days=search_raw.get(
            "fulltext_status_ttl_days", s.search.fulltext_status_ttl_days),
        sources=search_raw.get("sources", s.search.sources),
    )

    reader_raw = raw.get("reader", {})
    s.reader = ReaderConfig(**{k: reader_raw.get(k, getattr(s.reader, k)) for k in [
        "pdf_dir", "structure_backend", "assets_dir", "read_chunk_chars"]})

    storage_raw = raw.get("storage", {})
    s.storage = StorageConfig(**{k: storage_raw.get(k, getattr(s.storage, k)) for k in ["sqlite_path", "chroma_dir", "embedding_model", "reranker_model"]})

    return s


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
