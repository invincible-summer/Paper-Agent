"""Europe PMC search backend - free REST API, biomedicine / life sciences + preprints
(DESIGN D-083). Covers PubMed content (via pmid) plus preprints and OA full text.
Returns abstracts + OA PDF links. No API key needed.
"""
from __future__ import annotations

import httpx

from core.models import Paper
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter

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
            pdf_url = _extract_pdf_url(item)
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
                pdf_url=pdf_url,
                keywords=[],
                urls=urls,
            )
            papers.append(paper)
        return papers


def _extract_pdf_url(item: dict) -> str | None:
    """OA PDF links only — subscription/paywalled links are never returned,
    so the fetcher can never follow a non-OA publisher URL."""
    urls = (item.get("fullTextUrlList") or {}).get("fullTextUrl", []) or []
    oa_pdf = None
    for link in urls:
        style = (link.get("documentStyle") or "").lower()
        if style != "pdf":
            continue
        avail = (link.get("availability") or "").lower()
        if "open access" in avail or (link.get("availabilityCode") or "") == "OA":
            return link.get("url")
        if "oa" in avail and not oa_pdf:
            oa_pdf = link.get("url")
    return oa_pdf
