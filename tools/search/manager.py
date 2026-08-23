"""Source-routed, deadline-bounded paper search manager."""
from __future__ import annotations

import asyncio
import time

from core.models import Paper
from core.paper_search_settings_store import source_capability_enabled
from tools.search.base import SearchBackend, SearchOutcome, normalize_title, title_similarity
from tools.search.registry import runtime_gate
from tools.search.router import RouteHints, choose_sources, infer_route_hints
from tools.search.openalex import OpenAlexBackend
from tools.search.semantic_scholar import SemanticScholarBackend
from tools.search.arxiv import ArxivBackend
from tools.search.crossref import CrossrefBackend
from tools.search.europepmc import EuropePmcBackend
from tools.search.doaj import DoajBackend
from tools.search.hal import HalBackend
from tools.search.openaire import OpenAireBackend
from tools.search.core import CoreBackend
from tools.search.rxiv import RxivBackend
from tools.search.pubmed import PubMedBackend
from tools.search.datacite import DataCiteBackend
from tools.search.dblp import DblpBackend

SEARCH_DEADLINE_SECONDS = 30.0
PER_SOURCE_TIMEOUT_SECONDS = 12.0
FALLBACK_MIN_RESULTS = 8
FALLBACK_MIN_REMAINING_SECONDS = 8.0

BACKENDS: dict[str, SearchBackend] = {
    "openalex": OpenAlexBackend(), "semantic_scholar": SemanticScholarBackend(),
    "arxiv": ArxivBackend(), "crossref": CrossrefBackend(),
    "europepmc": EuropePmcBackend(), "doaj": DoajBackend(),
    "hal": HalBackend(), "openaire": OpenAireBackend(), "core": CoreBackend(),
    "biorxiv": RxivBackend("biorxiv"), "medrxiv": RxivBackend("medrxiv"),
    "pubmed": PubMedBackend(), "datacite": DataCiteBackend(), "dblp": DblpBackend(),
}

_FAILURE_CODES = {"rate_limited", "timeout", "connection_error", "server_error", "schema_mismatch", "bot_challenge", "unexpected_redirect"}


class SearchManager:
    """Run one bounded batch task per selected source, then optional fallbacks."""

    def __init__(self, enabled_sources: list[str] | None = None, results_per_source: int = 20,
                 search_deadline_seconds: float | None = None,
                 per_source_timeout_seconds: float | None = None,
                 routing_mode: str = "smart"):
        self.enabled_sources = list(BACKENDS) if enabled_sources is None else list(enabled_sources)
        self.results_per_source = results_per_source
        requested_deadline = SEARCH_DEADLINE_SECONDS if search_deadline_seconds is None else float(search_deadline_seconds)
        self.search_deadline_seconds = min(30.0, requested_deadline)
        self.per_source_timeout_seconds = PER_SOURCE_TIMEOUT_SECONDS if per_source_timeout_seconds is None else float(per_source_timeout_seconds)
        self.routing_mode = routing_mode if routing_mode in {"smart", "all_enabled"} else "smart"
        self.last_outcomes: list[SearchOutcome] = []
        self.last_route: dict = {}

    async def _bounded_search(self, source: str, backend: SearchBackend,
                              queries: list[str]) -> SearchOutcome:
        started = time.monotonic()
        try:
            if hasattr(backend, "search_many"):
                outcome = await asyncio.wait_for(
                    backend.search_many(queries, self.results_per_source),
                    timeout=self.per_source_timeout_seconds,
                )
            else:  # compatibility for injected extension/test backends
                papers = []
                for query in queries[:2]:
                    papers.extend(await backend.search(query, self.results_per_source))
                outcome = SearchOutcome(source, papers, "ok" if papers else "reachable_empty", request_count=min(2, len(queries)))
            outcome.source = source
            abstract_allowed, abstract_reason = source_capability_enabled(
                source, "abstract"
            )
            for paper in outcome.papers:
                if paper.abstract and not paper.abstract_source:
                    paper.abstract_source = source
                if not abstract_allowed:
                    paper.abstract = ""
                    paper.abstract_source = source
                    paper.abstract_policy_status = "disabled"
                    paper.abstract_policy_reason = (
                        abstract_reason or "source_capability_disabled"
                    )
            if not outcome.network_ms:
                outcome.network_ms = int((time.monotonic() - started) * 1000)
            return outcome
        except asyncio.TimeoutError:
            return SearchOutcome(source, [], "local_budget_exhausted", network_ms=int((time.monotonic()-started)*1000), error_code="local_budget_exhausted")
        except asyncio.CancelledError:
            raise
        except Exception:
            return SearchOutcome(source, [], "schema_mismatch", network_ms=int((time.monotonic()-started)*1000), error_code="schema_mismatch")

    async def _run_wave(self, sources: list[str], queries: list[str], deadline: float, report) -> list[SearchOutcome]:
        tasks: dict[asyncio.Task, str] = {}
        for source in sources:
            backend = BACKENDS.get(source)
            if backend is None: continue
            allowed, reason = source_capability_enabled(source, "search")
            if not allowed:
                report(f"  → {source} 搜索能力已关闭，跳过：{reason or '管理员策略'}")
                continue
            tasks[asyncio.create_task(self._bounded_search(source, backend, queries))] = source
        outcomes: list[SearchOutcome] = []
        pending = set(tasks)
        while pending:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0: break
            done, pending = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if not done: break
            for task in done:
                outcome = task.result(); outcomes.append(outcome)
                report(f"  → {outcome.source} {outcome.status}（{len(outcome.papers)} 篇，{outcome.network_ms}ms）")
        if pending:
            for task in pending: task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in pending:
                source = tasks[task]
                outcome = SearchOutcome(source, [], "local_budget_exhausted", error_code="local_budget_exhausted")
                outcomes.append(outcome)
                report(f"  → {source} 达到本轮总预算，取消且不计远端故障")
        return outcomes

    async def search_all(self, queries: list[str], progress_callback=None,
                         route_hints: RouteHints | dict | None = None,
                         topic: str = "", deadline: float | None = None) -> list[Paper]:
        report = progress_callback or (lambda _msg: None)
        seen = set(); unique_queries=[]
        for query in queries:
            norm=normalize_title(query)
            if norm and norm not in seen:
                seen.add(norm); unique_queries.append(query)
            if len(unique_queries) >= 8: break
        queries = unique_queries or ([topic] if topic else [])
        if not queries: return []

        eligible = {s for s in self.enabled_sources if runtime_gate(s)[0] and source_capability_enabled(s, "search")[0]}
        if self.routing_mode == "all_enabled":
            primary=[s for s in self.enabled_sources if s in eligible]; fallback=[]
            reasons={s:"管理员全启用模式" for s in primary}
        else:
            if isinstance(route_hints, dict): route_hints = RouteHints(**{k:v for k,v in route_hints.items() if k in RouteHints.__dataclass_fields__})
            hints=infer_route_hints(" ".join([topic,*queries]), route_hints)
            decision=choose_sources(hints, self.enabled_sources, eligible)
            primary,fallback,reasons=decision.primary,decision.fallback,decision.reasons
        ineligible = {}
        skipped = []
        for source in self.enabled_sources:
            if source in eligible:
                continue
            configured, config_reason = runtime_gate(source)
            if not configured:
                reason_code, reason = config_reason, config_reason
            else:
                _allowed, reason = source_capability_enabled(source, "search")
                reason_code = "source_capability_disabled"
            ineligible[source] = reason_code
            skipped.append({"source": source, "capability": "search", "status": "skipped",
                            "reason_code": reason_code, "reason": reason or reason_code})
        self.last_route={"primary":primary,"fallback":fallback,"reasons":reasons,
                         "ineligible":ineligible, "skipped":skipped}
        report(f"  → 智能路由主渠道：{', '.join(primary) if primary else '无'}")
        if "pubmed" in primary or "pubmed" in fallback:
            report("  → PubMed/NLM 仅提供来源记录，不代表 NLM 对内容或结论背书；请核对原始记录。")
        local_deadline = asyncio.get_running_loop().time() + self.search_deadline_seconds
        deadline = min(local_deadline, deadline) if deadline is not None else local_deadline
        outcomes=await self._run_wave(primary,queries,deadline,report)
        papers=[p for o in outcomes for p in o.papers]
        deduped=self._dedup(papers)
        if fallback and len(deduped)<FALLBACK_MIN_RESULTS and deadline-asyncio.get_running_loop().time()>FALLBACK_MIN_REMAINING_SECONDS:
            report(f"  → 主渠道仅 {len(deduped)} 篇，启动兜底：{', '.join(fallback)}")
            more=await self._run_wave(fallback,queries,deadline,report); outcomes.extend(more)
            papers.extend(p for o in more for p in o.papers); deduped=self._dedup(papers)
        self.last_outcomes=outcomes
        report(f"  → Dedup: {len(papers)} → {len(deduped)} unique papers")
        return deduped
    def _dedup(self, papers: list[Paper]) -> list[Paper]:
        by_id: dict[str, Paper] = {}
        by_doi: dict[str, str] = {}
        by_norm_title: dict[str, str] = {}
        for paper in papers:
            if paper.doi and paper.doi in by_doi:
                _merge(by_id[by_doi[paper.doi]], paper)
                continue
            norm = normalize_title(paper.title)
            matched_id = by_norm_title.get(norm)
            if matched_id is None:
                for existing_norm, existing_id in by_norm_title.items():
                    if title_similarity(norm, existing_norm) >= 0.95:
                        matched_id = existing_id
                        break
            if matched_id:
                existing = by_id[matched_id]
                _merge(existing, paper)
                if paper.doi and not existing.doi:
                    existing.doi = paper.doi
                    by_doi[paper.doi] = existing.id
                continue
            by_id[paper.id] = paper
            if paper.doi:
                by_doi[paper.doi] = paper.id
            by_norm_title[norm] = paper.id
        return list(by_id.values())


def _merge(existing: Paper, new: Paper) -> None:
    if new.abstract and len(new.abstract) > len(existing.abstract):
        existing.abstract = new.abstract
        existing.abstract_source = new.abstract_source or new.source
        existing.abstract_policy_status = new.abstract_policy_status
        existing.abstract_policy_reason = new.abstract_policy_reason
    if new.citation_count > existing.citation_count:
        existing.citation_count = new.citation_count
    if new.doi and not existing.doi:
        existing.doi = new.doi
    if new.keywords:
        existing.keywords = list(set(existing.keywords + new.keywords))
    if new.urls:
        existing.urls.update(new.urls)
    sources = set(existing.source.split(",")) if existing.source else set()
    sources.add(new.source)
    existing.source = ",".join(sorted(sources))
