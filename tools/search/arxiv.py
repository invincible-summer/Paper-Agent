"""arXiv search backend - free API, preprints (DESIGN D-011)."""

from __future__ import annotations

import feedparser
import httpx

from core.models import Paper
from tools.search.base import SearchBackend, generate_paper_id, is_chinese, RateLimiter

API_URL = "https://export.arxiv.org/api/query"

# arXiv ToU hard limit (https://info.arxiv.org/help/api/tou.html):
# no more than one request every 3 seconds, single connection.
_limiter = RateLimiter(max_concurrent=1, min_interval=3.0, source_name="arxiv", fast_fail_429=True)


class ArxivBackend(SearchBackend):
    name = "arxiv"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        # arXiv is English-only, skip Chinese queries
        if is_chinese(query):
            return []
        # arXiv API doesn't handle long queries well - use ti: (title) and abs: (abstract) fields
        # Build a focused query: take key terms and search in title + abstract
        # arXiv API works best with simple all: search, but needs shorter queries
        terms = query.split()
        if len(terms) > 6:
            terms = terms[:6]
        search_query = "all:" + " AND all:".join(terms)
        params = {
            "search_query": search_query,
            "start": 0,
            "max_results": min(limit, 50),
            "sortBy": "relevance",
        }
        try:
            # D-070: bypass Clash proxy — it drops arXiv/OpenAlex connections,
            # causing search_all to hang at ~half the tasks. Trust no env proxy.
            async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
                async with _limiter:
                    resp = await _limiter.fetch(client, "GET", API_URL, params=params)
                if resp.status_code != 200:
                    return []
                xml = resp.text
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

        feed = feedparser.parse(xml)
        papers: list[Paper] = []
        for entry in feed.entries:
            title = entry.get("title", "").strip().replace("\n", " ")
            authors = [a.get("name", "") for a in entry.get("authors", [])]
            year = None
            if entry.get("published"):
                year = int(entry["published"][:4])

            # Extract DOI if present
            doi = None
            for link in entry.get("links", []):
                if "doi.org" in link.get("href", ""):
                    doi = link["href"].replace("https://doi.org/", "")

            # PDF link
            pdf_url = None
            for link in entry.get("links", []):
                if link.get("title") == "pdf":
                    pdf_url = link.get("href")

            abstract = entry.get("summary", "").strip().replace("\n", " ")
            first_author = authors[0] if authors else ""
            arxiv_url = entry.get("id", "")

            paper = Paper(
                id=generate_paper_id(title, first_author, year, doi),
                title=title,
                authors=authors,
                year=year,
                venue="arXiv",
                doi=doi,
                source=self.name,
                language="en",
                citation_count=0,
                abstract=abstract,
                pdf_url=pdf_url,
                keywords=_extract_categories(entry),
                urls={"arxiv": arxiv_url},
            )
            papers.append(paper)
        return papers


def _extract_categories(entry) -> list[str]:
    tags = entry.get("tags", [])
    return [t.get("term", "") for t in tags[:5] if t.get("term")]
