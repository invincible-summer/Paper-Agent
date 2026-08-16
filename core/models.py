"""Core data models (Paper, PaperSummary, etc.) per DESIGN.md chapter 12."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Reference:
    """A single reference entry extracted from a paper's reference list."""
    raw_text: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    source_paper_id: str | None = None  # resolved paper_id if in our library


@dataclass
class Paper:
    """Paper metadata - Search Agent output (DESIGN 12.1)."""
    id: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    doi: str | None = None
    source: str = ""            # openalex / arxiv / semantic_scholar / ...
    language: str = "en"
    citation_count: int = 0
    abstract: str = ""
    pdf_url: str | None = None
    pdf_path: str | None = None
    keywords: list[str] = field(default_factory=list)
    urls: dict[str, str] = field(default_factory=dict)
    relevance_score: float = -1.0     # LLM relevance score (-1 = not scored)
    layer: str = "reference"           # reference / search / core (DESIGN D-035)
    llm_reasoning: str = ""            # LLM scoring rationale (Pro mode, DESIGN D-035)
    fulltext_status: str = "unknown"   # unknown | available | unavailable（已验证的 OA PDF 可获取状态）

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "source": self.source,
            "language": self.language,
            "citation_count": self.citation_count,
            "abstract": self.abstract,
            "pdf_url": self.pdf_url,
            "pdf_path": self.pdf_path,
            "keywords": self.keywords,
            "urls": self.urls,
            "relevance_score": self.relevance_score,
            "layer": self.layer,
            "llm_reasoning": self.llm_reasoning,
            "fulltext_status": self.fulltext_status,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Paper:
        kwargs = {}
        for k in ["id", "title", "authors", "year", "venue", "doi",
                  "source", "language", "citation_count", "abstract",
                  "pdf_url", "pdf_path", "keywords", "urls"]:
            v = d.get(k)
            if v is not None:
                kwargs[k] = v
        p = cls(**kwargs)
        p.relevance_score = d.get("relevance_score", -1.0)
        p.layer = d.get("layer", "reference")
        p.llm_reasoning = d.get("llm_reasoning", "")
        p.fulltext_status = d.get("fulltext_status", "unknown")
        return p


# All possible summary fields (DESIGN 12.3a)
ALL_SUMMARY_FIELDS = [
    "research_problem", "methodology", "key_findings", "contributions",
    "limitations", "datasets", "baselines", "theoretical_framework",
    "sample_size", "interventions", "outcomes", "future_work",
]

# Predefined field profiles (DESIGN 12.3a)
FIELD_PROFILES: dict[str, list[str]] = {
    "cs": ["research_problem", "methodology", "key_findings",
           "contributions", "datasets", "baselines", "limitations"],
    "social_science": ["research_problem", "theoretical_framework",
                       "methodology", "key_findings", "contributions",
                       "limitations"],
    "medical": ["research_problem", "methodology", "key_findings",
                "sample_size", "interventions", "outcomes", "limitations"],
    "humanities": ["research_problem", "theoretical_framework",
                   "methodology", "key_findings", "contributions"],
    "general": ["research_problem", "methodology", "key_findings",
                "contributions", "limitations", "future_work"],
}


@dataclass
class CustomField:
    name: str
    display_name: str
    description: str
    field_type: str = "str"  # "str" | "list[str]"


@dataclass
class PaperSummary:
    """Structured summary - Reader Agent output (DESIGN 12.2)."""
    paper_id: str = ""
    research_problem: str = ""
    methodology: str = ""
    key_findings: list[str] = field(default_factory=list)
    contributions: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    baselines: list[str] = field(default_factory=list)
    theoretical_framework: str = ""
    sample_size: str = ""
    interventions: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    future_work: list[str] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    full_text: str | None = None
    embedding: list[float] | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # custom fields
    # Lightweight refs to this paper's understood elements (figures/tables/
    # formulas). Full understanding/bbox/asset live in the global paper_elements
    # table; summaries carry only short refs so session JSON stays compact.
    elements: list[dict] = field(default_factory=list)
    # Lightweight structural metadata: enough to answer deterministic outline
    # questions and explain whether full-text/OCR actually succeeded, without
    # duplicating raw PDF bytes or filesystem paths.
    section_outline: list[dict[str, Any]] = field(default_factory=list)
    document_info: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (inverse of summaries_from_dicts)."""
        d: dict[str, Any] = {"paper_id": self.paper_id}
        for f in [
            "research_problem", "methodology", "theoretical_framework",
            "sample_size", "key_findings", "contributions", "limitations",
            "datasets", "baselines", "interventions", "outcomes", "future_work",
        ]:
            val = getattr(self, f, None)
            if val not in (None, "", []):
                d[f] = val
        if self.references:
            d["references_raw"] = [r.raw_text for r in self.references[:50]]
        if self.full_text:
            d["full_text"] = self.full_text
        if self.elements:
            d["elements"] = self.elements
        if self.section_outline:
            d["section_outline"] = self.section_outline
        if self.document_info:
            d["document_info"] = self.document_info
        return d

def summaries_from_dicts(raw: dict[str, dict]) -> dict[str, "PaperSummary"]:
    """Convert plain dicts (from JSON/API) to PaperSummary objects.

    Handles the case where paper_summaries come from the frontend as dicts
    rather than PaperSummary dataclass instances.
    """
    from core.models import PaperSummary, Reference
    result: dict[str, PaperSummary] = {}
    for pid, s in raw.items():
        if isinstance(s, PaperSummary):
            result[pid] = s
            continue
        if not isinstance(s, dict):
            continue
        summary = PaperSummary(paper_id=s.get("paper_id", pid))
        for field in ["research_problem", "methodology", "theoretical_framework",
                      "sample_size", "key_findings", "contributions",
                      "limitations", "datasets", "baselines",
                      "interventions", "outcomes", "future_work"]:
            val = s.get(field)
            if val is not None and hasattr(summary, field):
                setattr(summary, field, val)
        refs_raw = s.get("references_raw", s.get("references", []))
        if refs_raw:
            summary.references = [Reference(raw_text=r) if isinstance(r, str) else Reference(raw_text=str(r)) for r in refs_raw[:50]]
        elems = s.get("elements", [])
        if isinstance(elems, list):
            summary.elements = elems
        outline = s.get("section_outline", [])
        if isinstance(outline, list):
            summary.section_outline = outline
        info = s.get("document_info", {})
        if isinstance(info, dict):
            summary.document_info = info
        result[pid] = summary
    return result
