"""HAL search backend - French national open repository (hyper articles en ligne).
Free API, no key. Strong in humanities / social sciences / European theses.
All records are author-deposited OA copies; fileMain_s is a direct legal PDF.
"""
from __future__ import annotations

import httpx

from core.models import Paper
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter

API_URL = "https://api.archives-ouvertes.fr/search/"

_FIELDS = ("halId_s,title_s,abstract_s,authFullName_s,producedDateY_i,"
           "doiId_s,journalTitle_s,fileMain_s,keyword_s,docType_s")

_limiter = RateLimiter(max_concurrent=3, min_interval=0.5, source_name="hal", fast_fail_429=True)


def _first(value) -> str:
    """HAL fields are mostly lists; take the first non-empty item."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def parse_docs(data: dict) -> list[Paper]:
    """Parse a HAL JSON response into Papers (pure, offline-testable)."""
    papers: list[Paper] = []
    for doc in (data.get("response") or {}).get("docs", []) or []:
        title = _first(doc.get("title_s"))
        if not title:
            continue
        authors = [a for a in (doc.get("authFullName_s") or []) if a]
        year = doc.get("producedDateY_i")
        try:
            year = int(year) if year else None
        except (ValueError, TypeError):
            year = None
        doi = _first(doc.get("doiId_s")) or None
        hal_id = _first(doc.get("halId_s"))
        urls = {"hal": f"https://hal.science/{hal_id}"} if hal_id else {}
        papers.append(Paper(
            id=generate_paper_id(title, authors[0] if authors else "", year, doi),
            title=title,
            authors=authors,
            year=year,
            venue=_first(doc.get("journalTitle_s")),
            doi=doi,
            source="hal",
            language="en",
            citation_count=0,  # HAL has no citation counts
            abstract=_first(doc.get("abstract_s")),
            pdf_url=_first(doc.get("fileMain_s")) or None,  # OA repository PDF
            keywords=[k for k in (doc.get("keyword_s") or []) if isinstance(k, str)],
            urls=urls,
        ))
    return papers


class HalBackend(SearchBackend):
    name = "hal"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        query = shorten_chinese_query(query)
        params = {
            "q": query,
            "fl": _FIELDS,
            "rows": min(limit, 50),
            "wt": "json",
        }
        try:
            async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
                async with _limiter:
                    resp = await _limiter.fetch(client, "GET", API_URL, params=params)
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
        return parse_docs(data)
