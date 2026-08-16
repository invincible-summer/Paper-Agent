"""OpenAlex search backend - free API, 250M+ works (DESIGN D-011, D-047)."""

from __future__ import annotations

import httpx

from core.models import Paper
from core.config import get_settings
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter

API_URL = "https://api.openalex.org/works"

# Concurrency: 5 simultaneous, 0.5s min interval between requests
_limiter = RateLimiter(max_concurrent=5, min_interval=0.5)


class OpenAlexBackend(SearchBackend):
    name = "openalex"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        """Search OpenAlex with relevance sorting."""
        query = shorten_chinese_query(query)
        s = get_settings()
        email = s.search.openalex_email
        params = {
            "search": query,
            "sort": "relevance_score:desc",
            "per-page": min(limit, 50),
        }
        if email:
            # Polite pool only with a real configured email; otherwise the
            # anonymous standard pool — never send a placeholder identity.
            params["mailto"] = email
        try:
            # D-070: bypass the local Clash proxy — it throttles/drops academic
            # API connections, making search_all hang mid-flight (30/60 tasks).
            async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
                async with _limiter:
                    resp = await _limiter.fetch(client, "GET", API_URL, params=params)
                if resp.status_code != 200:
                    return []
                data = resp.json()
        except Exception:
            return []

        papers: list[Paper] = []
        for item in data.get("results", []):
            authors = [a["author"]["display_name"]
                       for a in item.get("authorships", [])
                       if a.get("author", {}).get("display_name")]
            doi_raw = item.get("doi") or ""
            doi = doi_raw.replace("https://doi.org/", "") if doi_raw else None

            abstract = ""
            inv_idx = item.get("abstract_inverted_index")
            if inv_idx:
                positions: list[tuple[int, str]] = []
                for word, idxs in inv_idx.items():
                    for pos in idxs:
                        positions.append((pos, word))
                positions.sort()
                abstract = " ".join(w for _, w in positions)

            title = item.get("title") or item.get("display_name") or ""
            year = item.get("publication_year")
            venue = ""
            venue_obj = item.get("primary_location", {}).get("source", {})
            if venue_obj:
                venue = venue_obj.get("display_name", "")

            first_author = authors[0] if authors else ""
            paper = Paper(
                id=generate_paper_id(title, first_author, year, doi),
                title=title,
                authors=authors,
                year=year,
                venue=venue,
                doi=doi,
                source=self.name,
                citation_count=item.get("cited_by_count", 0),
                abstract=abstract,
                pdf_url=_extract_pdf_url(item),
                keywords=_extract_keywords(item),
                urls={"openalex": item.get("id", "")},
            )
            papers.append(paper)
        return papers


def _extract_pdf_url(item: dict) -> str | None:
    loc = item.get("primary_location") or {}
    pdf = loc.get("pdf_url")
    if pdf:
        return pdf
    oa = item.get("open_access") or {}
    return oa.get("oa_url")


def _extract_keywords(item: dict) -> list[str]:
    concepts = item.get("concepts") or []
    return [c.get("display_name", "") for c in concepts[:5] if c.get("display_name")]
