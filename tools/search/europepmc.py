"""Europe PMC metadata and abstract search backend."""
from __future__ import annotations

import httpx

from core.models import Paper
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter
from tools.search.http_client import get_search_http_client

API_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

_limiter = RateLimiter(max_concurrent=3, min_interval=0.5, source_name="europepmc", fast_fail_429=True)


class EuropePmcBackend(SearchBackend):
    name = "europepmc"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        query = shorten_chinese_query(query)
        params = {
            "query": query,
            "format": "json",
            "pageSize": min(limit, 50),
            "resultType": "core",
            "sort": "CITED desc",
        }
        try:
            client = get_search_http_client()
            async with _limiter:
                resp = await _limiter.fetch(client, "GET", API_URL, params=params)
            self._capture_response(resp)
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, dict) or not isinstance((data.get("resultList") or {}).get("result"), list):
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
        for item in data.get("resultList", {}).get("result", []):
            title = item.get("title") or ""
            if not title:
                continue
            authors = [a.strip() for a in (item.get("authorString") or "").split(",") if a.strip()]
            year = None
            py = item.get("pubYear")
            if py:
                try:
                    year = int(py)
                except (ValueError, TypeError):
                    pass
            venue = (item.get("journalInfo") or {}).get("journal", {}).get("title", "") or ""
            doi = item.get("doi")
            pmid = item.get("pmid")
            citation_count = item.get("citedByCount", 0) or 0
            abstract = item.get("abstractText", "") or ""
            urls = {}
            src = item.get("source") or "MED"
            if item.get("id"):
                urls["europepmc"] = f"https://europepmc.org/article/{src}/{item['id']}"
            if pmid:
                urls["pmid"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
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
                keywords=[],
                urls=urls,
            )
            papers.append(paper)
        return papers
