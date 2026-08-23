"""PubMed backend using only the official NCBI E-utilities APIs.

Search and abstract retrieval are deliberately separate.  Ordinary routing
uses ESearch + ESummary when the persistent abstract capability is disabled,
so it can return title/author/DOI metadata without issuing the independent
EFetch abstract request.  Administrator diagnostics explicitly use
ESearch + EFetch to force-test the abstract phase even while its gate is closed.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from core.config import get_settings
from core.models import Paper
from tools.search.base import RateLimiter, SearchBackend, generate_paper_id
from tools.search.http_client import get_search_http_client
from tools.search.registry import contact_email

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_LIMITER_NO_KEY = RateLimiter(
    max_concurrent=1, min_interval=0.34, fast_fail_429=True
)
_LIMITER_KEY = RateLimiter(
    max_concurrent=2, min_interval=0.11, fast_fail_429=True
)


def parse_pubmed_xml(xml: str) -> list[Paper]:
    root = ET.fromstring(xml)
    papers: list[Paper] = []
    for article in root.findall(".//PubmedArticle"):
        medline = article.find("MedlineCitation")
        record = medline.find("Article") if medline is not None else None
        if record is None:
            continue
        title_node = record.find("ArticleTitle")
        title = (
            "".join(title_node.itertext()).strip()
            if title_node is not None else ""
        )
        if not title:
            continue
        authors: list[str] = []
        for author in record.findall(".//AuthorList/Author"):
            collective = author.findtext("CollectiveName")
            name = collective or " ".join(filter(None, [
                author.findtext("ForeName"), author.findtext("LastName"),
            ]))
            if name.strip():
                authors.append(name.strip())
        year = None
        for path in ("Journal/JournalIssue/PubDate/Year", "ArticleDate/Year"):
            raw = record.findtext(path)
            if raw and raw[:4].isdigit():
                year = int(raw[:4])
                break
        doi = None
        for identifier in article.findall(".//ArticleIdList/ArticleId"):
            if (identifier.attrib.get("IdType") or "").lower() == "doi":
                doi = (identifier.text or "").strip().lower()
        pmid = medline.findtext("PMID") if medline is not None else ""
        abstract = " ".join(
            "".join(node.itertext()).strip()
            for node in record.findall(".//Abstract/AbstractText")
        ).strip()
        venue = record.findtext("Journal/Title") or ""
        papers.append(Paper(
            id=generate_paper_id(
                title, authors[0] if authors else "", year, doi
            ),
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            source="pubmed",
            abstract=abstract,
            urls={
                "pubmed": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            } if pmid else {},
        ))
    return papers


def parse_pubmed_summary(payload: dict) -> list[Paper]:
    """Parse the documented ESummary JSON shape into metadata-only papers."""
    result = payload.get("result") if isinstance(payload, dict) else None
    uids = result.get("uids") if isinstance(result, dict) else None
    if not isinstance(uids, list):
        raise ValueError("invalid_esummary_schema")
    papers: list[Paper] = []
    for uid in uids:
        item = result.get(str(uid))
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        authors = [
            str(author.get("name") or "").strip()
            for author in item.get("authors") or []
            if isinstance(author, dict) and author.get("name")
        ]
        pubdate = str(item.get("pubdate") or "")
        year = int(pubdate[:4]) if pubdate[:4].isdigit() else None
        doi = None
        for identifier in item.get("articleids") or []:
            if not isinstance(identifier, dict):
                continue
            id_type = str(
                identifier.get("idtype") or identifier.get("idtypen") or ""
            ).lower()
            if id_type == "doi":
                doi = str(identifier.get("value") or "").strip().lower() or None
                break
        papers.append(Paper(
            id=generate_paper_id(
                title, authors[0] if authors else "", year, doi
            ),
            title=title,
            authors=authors,
            year=year,
            venue=str(
                item.get("fulljournalname") or item.get("source") or ""
            ),
            doi=doi,
            source="pubmed",
            abstract="",
            urls={
                "pubmed": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/"
            },
        ))
    return papers


class PubMedBackend(SearchBackend):
    name = "pubmed"

    def _request_context(self):
        settings = get_settings().search
        email = contact_email(settings)
        if not email:
            return None
        common = {"db": "pubmed", "tool": "PaperAgent", "email": email}
        api_key = getattr(settings, "ncbi_api_key", "") or ""
        if api_key:
            common["api_key"] = api_key
        limiter = _LIMITER_KEY if api_key else _LIMITER_NO_KEY
        return common, limiter

    async def _search_ids(
        self, query: str, limit: int, client, common: dict, limiter,
    ) -> list[str] | None:
        async with limiter:
            response = await limiter.fetch(
                client,
                "GET",
                ESEARCH_URL,
                params={
                    **common,
                    "term": query,
                    "retmode": "json",
                    "retmax": min(limit, 50),
                },
            )
        self._capture_response(response)
        if response.status_code != 200:
            return None
        payload = response.json()
        result = payload.get("esearchresult") if isinstance(payload, dict) else None
        ids = result.get("idlist") if isinstance(result, dict) else None
        if not isinstance(ids, list):
            self._forced_status = "schema_mismatch"
            return None
        return [str(identifier) for identifier in ids if str(identifier)]

    async def _fetch_summaries(
        self, ids: list[str], client, common: dict, limiter,
    ) -> list[Paper]:
        async with limiter:
            response = await limiter.fetch(
                client,
                "GET",
                ESUMMARY_URL,
                params={
                    **common,
                    "id": ",".join(ids),
                    "retmode": "json",
                },
            )
        self._capture_response(response)
        if response.status_code != 200:
            return []
        return parse_pubmed_summary(response.json())

    async def _fetch_details(
        self, ids: list[str], client, common: dict, limiter,
    ) -> list[Paper]:
        async with limiter:
            response = await limiter.fetch(
                client,
                "GET",
                EFETCH_URL,
                params={
                    **common,
                    "id": ",".join(ids),
                    "retmode": "xml",
                },
            )
        self._capture_response(response)
        return (
            parse_pubmed_xml(response.text)
            if response.status_code == 200 else []
        )

    async def _search(
        self, query: str, limit: int, *, include_abstract: bool,
    ) -> list[Paper]:
        context = self._request_context()
        if context is None:
            return []
        common, limiter = context
        client = get_search_http_client()
        try:
            ids = await self._search_ids(
                query, limit, client, common, limiter
            )
            if not ids:
                return []
            if include_abstract:
                return await self._fetch_details(
                    ids, client, common, limiter
                )
            return await self._fetch_summaries(
                ids, client, common, limiter
            )
        except httpx.TimeoutException:
            self._forced_status = "timeout"
            return []
        except httpx.RequestError:
            self._forced_status = "connection_error"
            return []
        except (ET.ParseError, ValueError, TypeError):
            self._forced_status = "schema_mismatch"
            return []

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        from core.paper_search_settings_store import source_capability_enabled

        include_abstract = source_capability_enabled(
            "pubmed", "abstract"
        )[0]
        return await self._search(
            query, limit, include_abstract=include_abstract
        )

    async def search_for_diagnostic(
        self, query: str, limit: int = 20,
    ) -> list[Paper]:
        """Administrator-only forced abstract phase, bypassing capability gate."""
        return await self._search(query, limit, include_abstract=True)
