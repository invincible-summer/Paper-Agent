"""DOAJ search backend - free API, OA journals across ALL disciplines, strong in
humanities / social sciences (DESIGN D-083). ~8M OA articles. No API key needed.
"""
from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from core.models import Paper
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter

API_URL = "https://doaj.org/api/search/articles"

# DOAJ is small infra: 2 concurrent, 1.0s between requests
_limiter = RateLimiter(max_concurrent=2, min_interval=1.0, source_name="doaj", fast_fail_429=True)


class DoajBackend(SearchBackend):
    name = "doaj"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        query = shorten_chinese_query(query)
        # DOAJ search is path-based: /api/search/articles/{query}
        url = f"{API_URL}/{quote(query)}"
        params = {"page": 1, "pageSize": min(limit, 50)}
        try:
            async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
                async with _limiter:
                    resp = await _limiter.fetch(client, "GET", url, params=params)
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

        papers: list[Paper] = []
        for item in data.get("results", []):
            bib = item.get("bibjson", {})
            title = bib.get("title") or ""
            if not title:
                continue
            authors = [a.get("name", "") for a in bib.get("author", []) if a.get("name")]
            year = None
            y = bib.get("year")
            if y:
                try:
                    year = int(y)
                except (ValueError, TypeError):
                    pass
            venue = (bib.get("journal") or {}).get("title", "") or ""
            doi = None
            for ident in bib.get("identifier", []):
                if (ident.get("type") or "").lower() == "doi":
                    doi = ident.get("id")
                    break
            abstract = _strip_html(bib.get("abstract") or "")
            pdf_url = _extract_pdf_url(bib)
            keywords = [kw for kw in bib.get("keywords", []) if isinstance(kw, str)]
            first_author = authors[0] if authors else ""
            doaj_id = item.get("id")
            paper = Paper(
                id=generate_paper_id(title, first_author, year, doi),
                title=title,
                authors=authors,
                year=year,
                venue=venue,
                doi=doi,
                source=self.name,
                language="en",
                citation_count=0,  # DOAJ has no citation counts
                abstract=abstract,
                pdf_url=pdf_url,
                keywords=keywords,
                urls={"doaj": f"https://doaj.org/article/{doaj_id}"} if doaj_id else {},
            )
            papers.append(paper)
        return papers


def _strip_html(text: str) -> str:
    if not text or "<" not in text:
        return text
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_pdf_url(bib: dict) -> str | None:
    # Prefer a PDF fulltext link, fall back to any fulltext link.
    for link in bib.get("link", []) or []:
        if (link.get("type") or "").lower() == "fulltext" and (link.get("content_type") or "").lower() == "pdf":
            return link.get("url")
    for link in bib.get("link", []) or []:
        if (link.get("type") or "").lower() == "fulltext":
            return link.get("url")
    return None
