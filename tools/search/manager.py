"""Dedup + ranking search manager (DESIGN D-006, D-011)."""

from __future__ import annotations

import asyncio

from core.models import Paper
from tools.search.base import (
    SearchBackend,
    normalize_title,
    title_similarity,
)
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


BACKENDS: dict[str, SearchBackend] = {
    "openalex": OpenAlexBackend(),
    "semantic_scholar": SemanticScholarBackend(),
    "arxiv": ArxivBackend(),
    "crossref": CrossrefBackend(),
    "europepmc": EuropePmcBackend(),
    "doaj": DoajBackend(),
    "hal": HalBackend(),
    "openaire": OpenAireBackend(),
    "core": CoreBackend(),
}


class SearchManager:
    """Orchestrates multi-source search, dedup, and ranking."""

    def __init__(
        self,
        enabled_sources: list[str] | None = None,
        results_per_source: int = 20,
    ):
        self.enabled_sources = enabled_sources or list(BACKENDS.keys())
        self.results_per_source = results_per_source

    async def search_all(self, queries: list[str], progress_callback=None) -> list[Paper]:
        """Run all backends in parallel for all queries, then dedup + rank."""
        def report(msg):
            if progress_callback:
                progress_callback(msg)

        # Dedup queries: remove exact duplicates and near-identical (same token set)
        seen = set()
        unique_queries = []
        for q in queries:
            norm = normalize_title(q)
            if norm not in seen:
                seen.add(norm)
                unique_queries.append(q)
        if len(unique_queries) < len(queries):
            report(f"  → Query dedup: {len(queries)} → {len(unique_queries)} unique queries")
        queries = unique_queries

        coroutines = []
        s2_delay = 0.0
        for source in self.enabled_sources:
            backend = BACKENDS.get(source)
            if not backend:
                continue
            for q in queries:
                if source == "semantic_scholar":
                    coroutines.append(self._delayed_search(backend, q, s2_delay))
                    s2_delay += 1.5
                else:
                    coroutines.append(backend.search(q, self.results_per_source))

        tasks = {asyncio.create_task(coro) for coro in coroutines}
        total = len(tasks)
        report(f"  → {total} search tasks dispatched across {len(self.enabled_sources)} sources")

        # D-070: global deadline so a single hung backend cannot stall the whole
        # turn. Explicit Tasks are required here: asyncio.as_completed() creates
        # wrapper coroutines, and leaving those wrappers un-awaited at the deadline
        # emits RuntimeWarning and leaves backend requests running in the background.
        # Cancel and drain every pending Task before returning partial results.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SEARCH_DEADLINE_SECONDS

        results: list[list[Paper]] = []
        completed = 0
        failed_or_empty = 0
        pending = tasks
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break
            for task in done:
                try:
                    result = task.result()
                except Exception:
                    result = []
                results.append(result)
                if not result:
                    failed_or_empty += 1
                completed += 1
                report(f"  → {completed}/{total} search tasks done ({len(result)} papers)")

        timed_out = len(pending)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for _ in range(timed_out):
                results.append([])
                completed += 1
                report(f"  → {completed}/{total} search tasks done (0 papers)")

        unavailable = failed_or_empty + timed_out
        if unavailable:
            report(f"  → {unavailable} task(s) timed out / empty; proceeding with partial results")

        all_papers: list[Paper] = []
        for result in results:
            all_papers.extend(result)

        deduped = self._dedup(all_papers)
        report(f"  → Dedup: {len(all_papers)} → {len(deduped)} unique papers")
        return deduped

    async def _delayed_search(self, backend, query: str, delay: float) -> list[Paper]:
        """Run a search after a delay to respect rate limits."""
        if delay > 0:
            await asyncio.sleep(delay)
        return await backend.search(query, self.results_per_source)

    def _dedup(self, papers: list[Paper]) -> list[Paper]:
        """Cross-source dedup pipeline (DESIGN 12.5).

        Step 1: DOI exact match
        Step 2: title normalization + fuzzy match (>= 0.95)
        Step 3: merge info from duplicates
        """
        by_id: dict[str, Paper] = {}
        by_doi: dict[str, str] = {}
        by_norm_title: dict[str, str] = {}

        for paper in papers:
            # Step 1: DOI match
            if paper.doi and paper.doi in by_doi:
                existing = by_id[by_doi[paper.doi]]
                _merge(existing, paper)
                continue

            # Step 2: title fuzzy match
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

            # Step 3: new paper
            by_id[paper.id] = paper
            if paper.doi:
                by_doi[paper.doi] = paper.id
            by_norm_title[norm] = paper.id

        return list(by_id.values())

def _merge(existing: Paper, new: Paper) -> None:
    """Merge info from a duplicate into the existing record (DESIGN 12.5)."""
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
