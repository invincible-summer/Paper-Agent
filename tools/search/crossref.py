"""Crossref search backend - free REST API, 130M+ DOI records across ALL disciplines
(DESIGN D-083). Covers humanities / social science / medicine / engineering that
arXiv (CS/physics) misses entirely. Polite pool via mailto; no API key needed.
"""
from __future__ import annotations

import re

import httpx

from core.models import Paper
from core.config import get_settings
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter

API_URL = "https://api.crossref.org/works"

# Polite pool: 5 concurrent, 0.5s between requests
_limiter = RateLimiter(max_concurrent=5, min_interval=0.5, source_name="crossref", fast_fail_429=True)

# Skip these Crossref record types - figure/table/dataset snippets that
# pollute literature search results (e.g. "Figure 10: ...").
_NOISE_TYPES = {"component", "dataset", "peer-review", "report-component"}


class CrossrefBackend(SearchBackend):
    name = "crossref"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        query = shorten_chinese_query(query)
        s = get_settings()
        email = s.search.crossref_email or s.search.openalex_email
        params = {
            "query": query,
            "rows": min(limit * 2, 50),  # over-fetch; we drop noise types
            "select": "DOI,title,author,published,published-print,published-online,"
                      "container-title,is-referenced-by-count,abstract,link,subject,type,URL",
        }
        if email:
            params["mailto"] = email  # polite pool; anonymous otherwise
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

        papers: list[Paper] = []
        for item in data.get("message", {}).get("items", []):
            if (item.get("type") or "") in _NOISE_TYPES:
                continue
            title = (item.get("title") or [""])[0]
            if not title:
                continue
            authors = [_author_name(a) for a in item.get("author", []) if _author_name(a)]
            year = _extract_year(item)
            venue = (item.get("container-title") or [""])[0] or ""
            doi = item.get("DOI")
            citation_count = item.get("is-referenced-by-count", 0) or 0
            abstract = _strip_jats(item.get("abstract", ""))
            pdf_url = _extract_pdf_url(item)
            keywords = item.get("subject") or []
            first_author = authors[0] if authors else ""
            paper = Paper(
                id=generate_paper_id(title, first_author, year, doi),
                title=title,
                authors=authors,
                year=year,
                venue=venue,
                doi=doi,
                source=self.name,
                language="en",
                citation_count=citation_count,
                abstract=abstract,
                pdf_url=pdf_url,
                keywords=keywords,
                urls={"crossref": item.get("URL") or ""},
            )
            papers.append(paper)
            if len(papers) >= limit:
                break
        return papers


def _author_name(a: dict) -> str:
    if a.get("name"):
        return a["name"]
    given = a.get("given", "")
    family = a.get("family", "")
    return f"{given} {family}".strip()


def _extract_year(item: dict) -> int | None:
    for key in ("published-print", "published-online", "published", "issued"):
        dp = (item.get(key) or {}).get("date-parts")
        if dp and dp[0] and dp[0][0]:
            try:
                return int(dp[0][0])
            except (ValueError, TypeError):
                continue
    return None


def _strip_jats(abstract: str) -> str:
    """Strip JATS XML tags from Crossref abstracts."""
    if not abstract:
        return ""
    text = re.sub(r"<[^>]+>", " ", abstract)
    return re.sub(r"\s+", " ", text).strip()


def _extract_pdf_url(item: dict) -> str | None:
    for link in item.get("link", []) or []:
        ct = (link.get("content-type") or "").lower()
        if "pdf" in ct:
            return link.get("URL")
    return None
