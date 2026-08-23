"""OpenAlex search backend - free API, 250M+ works (DESIGN D-011, D-047)."""

from __future__ import annotations

import httpx

from core.models import Paper
from core.config import get_settings
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter
from tools.search.http_client import get_search_http_client

API_URL = "https://api.openalex.org/works"

# Concurrency: 5 simultaneous, 0.5s min interval between requests
_limiter = RateLimiter(max_concurrent=5, min_interval=0.5, source_name="openalex", fast_fail_429=True)


class OpenAlexBackend(SearchBackend):
    name = "openalex"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        """Search OpenAlex with relevance sorting."""
        query = shorten_chinese_query(query)
        s = get_settings()
        api_key = getattr(s.search, "openalex_api_key", "")
        if not api_key:
            return []
        params = {
            "search": query,
            "sort": "relevance_score:desc",
            "per_page": min(limit, 50),
            "api_key": api_key,
        }
        try:
            # D-070: bypass the local Clash proxy — it throttles/drops academic
            # API connections, making search_all hang mid-flight (30/60 tasks).
            client = get_search_http_client()
            async with _limiter:
                resp = await _limiter.fetch(client, "GET", API_URL, params=params)
                self._capture_response(resp)
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                self._forced_status = "schema_mismatch"
                return []
        except httpx.TimeoutException:
            self._forced_status = "timeout"
            return []
        except httpx.RequestError:
            self._forced_status = "connection_error"
            return []
        except Exception:
            self._forced_status = "schema_mismatch"
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
                keywords=_extract_keywords(item),
                urls={"openalex": item.get("id", "")},
            )
            papers.append(paper)
        return papers


def _extract_keywords(item: dict) -> list[str]:
    concepts = item.get("concepts") or []
    return [c.get("display_name", "") for c in concepts[:5] if c.get("display_name")]
