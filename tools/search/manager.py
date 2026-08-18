"""Dedup + ranking search manager (DESIGN D-006, D-011)."""

from __future__ import annotations

import asyncio

from core.models import Paper
from core.search_source_health import get_search_health_registry
from tools.search.base import SearchBackend, normalize_title, title_similarity
from tools.search.openalex import OpenAlexBackend
from tools.search.semantic_scholar import SemanticScholarBackend
from tools.search.arxiv import ArxivBackend
from tools.search.crossref import CrossrefBackend
from tools.search.europepmc import EuropePmcBackend
from tools.search.doaj import DoajBackend
from tools.search.hal import HalBackend
from tools.search.openaire import OpenAireBackend
from tools.search.core import CoreBackend

SEARCH_DEADLINE_SECONDS = 120.0
PER_SOURCE_TIMEOUT_SECONDS = 30.0

BACKENDS: dict[str, SearchBackend] = {
    "openalex": OpenAlexBackend(), "semantic_scholar": SemanticScholarBackend(),
    "arxiv": ArxivBackend(), "crossref": CrossrefBackend(),
    "europepmc": EuropePmcBackend(), "doaj": DoajBackend(),
    "hal": HalBackend(), "openaire": OpenAireBackend(), "core": CoreBackend(),
}


class SearchManager:
    """Run enabled sources with bounded per-task and global deadlines."""

    def __init__(self, enabled_sources: list[str] | None = None,
                 results_per_source: int = 20,
                 search_deadline_seconds: float | None = None,
                 per_source_timeout_seconds: float | None = None):
        self.enabled_sources = list(BACKENDS) if enabled_sources is None else list(enabled_sources)
        self.results_per_source = results_per_source
        self.search_deadline_seconds = (SEARCH_DEADLINE_SECONDS if search_deadline_seconds is None
                                        else float(search_deadline_seconds))
        self.per_source_timeout_seconds = (PER_SOURCE_TIMEOUT_SECONDS if per_source_timeout_seconds is None
                                           else float(per_source_timeout_seconds))

    async def _bounded_search(self, source: str, backend: SearchBackend,
                              query: str) -> tuple[str, list[Paper], str | None]:
        try:
            papers = await asyncio.wait_for(
                backend.search(query, self.results_per_source),
                timeout=self.per_source_timeout_seconds,
            )
            source_health = get_search_health_registry().get(source)
            recent_error = source_health.get("last_error_code")
            if not papers and recent_error in {
                "rate_limited", "timeout", "connection_error", "server_error", "invalid_response"
            }:
                return source, [], str(recent_error)
            return source, papers, None
        except asyncio.TimeoutError:
            get_search_health_registry().record_failure(source, "timeout")
            return source, [], "timeout"
        except asyncio.CancelledError:
            raise
        except Exception:
            get_search_health_registry().record_failure(source, "invalid_response")
            return source, [], "invalid_response"

    async def search_all(self, queries: list[str], progress_callback=None) -> list[Paper]:
        def report(msg):
            if progress_callback:
                progress_callback(msg)

        seen: set[str] = set()
        unique_queries: list[str] = []
        for query in queries:
            norm = normalize_title(query)
            if norm not in seen:
                seen.add(norm)
                unique_queries.append(query)
        if len(unique_queries) < len(queries):
            report(f"  → Query dedup: {len(queries)} → {len(unique_queries)} unique queries")
        queries = unique_queries

        health = get_search_health_registry()
        tasks: set[asyncio.Task] = set()
        participating_sources = 0
        for source in self.enabled_sources:
            backend = BACKENDS.get(source)
            if backend is None:
                continue
            allowed, state, remaining = health.allow(source)
            if not allowed:
                suffix = f"，约 {remaining:.0f}s 后半开" if remaining else ""
                report(f"  → {source} 已熔断，跳过本次检索{suffix}")
                continue
            participating_sources += 1
            source_queries = queries[:1] if state == "half_open" else queries
            for query in source_queries:
                tasks.add(asyncio.create_task(self._bounded_search(source, backend, query)))

        total = len(tasks)
        report(f"  → {total} search tasks dispatched across {participating_sources} sources")
        if not tasks:
            return []

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.search_deadline_seconds
        results: list[list[Paper]] = []
        completed = 0
        failed = 0
        pending = tasks
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for task in done:
                source, result, error = task.result()
                results.append(result)
                completed += 1
                if error:
                    failed += 1
                    report(f"  → {completed}/{total} {source} {error}，返回部分结果")
                else:
                    report(f"  → {completed}/{total} {source} done ({len(result)} papers)")

        timed_out = len(pending)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            completed += timed_out
            report(f"  → 全局检索时限到达，取消 {timed_out} 个慢任务")
        if failed or timed_out:
            report(f"  → {failed + timed_out} task(s) failed/timed out; proceeding with partial results")

        all_papers = [paper for result in results for paper in result]
        deduped = self._dedup(all_papers)
        report(f"  → Dedup: {len(all_papers)} → {len(deduped)} unique papers")
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
    if new.citation_count > existing.citation_count:
        existing.citation_count = new.citation_count
    if new.pdf_url and not existing.pdf_url:
        existing.pdf_url = new.pdf_url
    if new.doi and not existing.doi:
        existing.doi = new.doi
    if new.keywords:
        existing.keywords = list(set(existing.keywords + new.keywords))
    if new.urls:
        existing.urls.update(new.urls)
    sources = set(existing.source.split(",")) if existing.source else set()
    sources.add(new.source)
    existing.source = ",".join(sorted(sources))
