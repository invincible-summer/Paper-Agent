"""CORE search backend - world's largest aggregator of OA repository copies
(core.ac.uk), 200M+ records. Free API key required (registration); the backend
silently disables itself when CORE_API_KEY is not configured.
downloadUrl is a CORE-hosted legal repository PDF.
"""
from __future__ import annotations

import logging

import httpx

from core.config import get_settings
from core.models import Paper
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter

logger = logging.getLogger(__name__)

API_URL = "https://api.core.ac.uk/v3/search/works"

_limiter = RateLimiter(max_concurrent=3, min_interval=0.5, source_name="core", fast_fail_429=True)
_warned_no_key = False


def parse_results(data: dict) -> list[Paper]:
    """Parse a CORE v3 search response into Papers (pure, offline-testable)."""
    papers: list[Paper] = []
    for item in data.get("results", []) or []:
        title = item.get("title") or ""
        if not title:
            continue
        authors = [a.get("name", "") for a in (item.get("authors") or []) if a.get("name")]
        year = item.get("yearPublished")
        try:
            year = int(year) if year else None
        except (ValueError, TypeError):
            year = None
        doi = item.get("doi") or None
        work_id = item.get("id")
        papers.append(Paper(
            id=generate_paper_id(title, authors[0] if authors else "", year, doi),
            title=title,
            authors=authors,
            year=year,
            venue=(item.get("publisher") or "") or "",
            doi=doi,
            source="core",
            language="en",
            citation_count=item.get("citationCount", 0) or 0,
            abstract=item.get("abstract") or "",
            pdf_url=item.get("downloadUrl") or None,  # CORE-hosted OA PDF
            keywords=[],
            urls={"core": f"https://core.ac.uk/works/{work_id}"} if work_id else {},
        ))
    return papers


class CoreBackend(SearchBackend):
    name = "core"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        global _warned_no_key
        api_key = getattr(get_settings().search, "core_api_key", "") or ""
        if not api_key:
            if not _warned_no_key:
                logger.info("CORE backend disabled: set CORE_API_KEY (free) to enable.")
                _warned_no_key = True
            return []

        query = shorten_chinese_query(query)
        params = {"q": query, "limit": min(limit, 50)}
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
                async with _limiter:
                    resp = await _limiter.fetch(
                        client, "GET", API_URL, params=params, headers=headers)
                if resp.status_code != 200:
                    return []
                data = resp.json()
        except httpx.TimeoutException:
            from core.search_source_health import get_search_health_registry
            get_search_health_registry().record_failure(self.name, "timeout")
            return []
        except httpx.RequestError:
            from core.search_source_health import get_search_health_registry
            get_search_health_registry().record_failure(self.name, "connection_error")
            return []
        except Exception:
            from core.search_source_health import get_search_health_registry
            get_search_health_registry().record_failure(self.name, "invalid_response")
            return []
        return parse_results(data)
