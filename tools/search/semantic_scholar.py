"""Semantic Scholar search backend - free API, 200M+ papers (DESIGN D-011, D-047)."""

from __future__ import annotations

import httpx

from core.models import Paper
from core.config import get_settings
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter

API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

FIELDS = (
    "paperId,title,abstract,year,venue,citationCount,authors,"
    "externalIds,openAccessPdf,url"
)

# S2 has strict rate limits: 3 concurrent, 1.5s min interval
_limiter = RateLimiter(max_concurrent=3, min_interval=1.5)


class SemanticScholarBackend(SearchBackend):
    name = "semantic_scholar"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        query = shorten_chinese_query(query)
        terms = query.split()
        if len(terms) > 8:
            query = " ".join(terms[:8])
        params = {
            "query": query,
            "limit": min(limit, 100),
            "fields": FIELDS,
        }
        s = get_settings()
        headers = {}
        if s.search.s2_api_key:
            headers["x-api-key"] = s.search.s2_api_key

        try:
            async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
                async with _limiter:
                    resp = await _limiter.fetch(
                        client, "GET", API_URL,
                        params=params, headers=headers or None,
                    )
                if resp.status_code != 200:
                    return []
                data = resp.json()
        except Exception:
            return []

        papers: list[Paper] = []
        for item in data.get("data", []):
            title = item.get("title") or ""
            authors = [a.get("name", "") for a in item.get("authors", [])]
            year = item.get("year")
            doi = item.get("externalIds", {}).get("DOI")
            pdf_info = item.get("openAccessPdf") or {}
            pdf_url = pdf_info.get("url") if pdf_info else None
            first_author = authors[0] if authors else ""

            paper = Paper(
                id=generate_paper_id(title, first_author, year, doi),
                title=title,
                authors=authors,
                year=year,
                venue=item.get("venue") or "",
                doi=doi,
                source=self.name,
                citation_count=item.get("citationCount", 0),
                abstract=item.get("abstract") or "",
                pdf_url=pdf_url,
                keywords=[],
                urls={"semantic_scholar": item.get("url") or ""},
            )
            papers.append(paper)
        return papers
