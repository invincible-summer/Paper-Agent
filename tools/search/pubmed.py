"""PubMed backend using only the official NCBI E-utilities APIs."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import quote

import httpx

from core.config import get_settings
from core.models import Paper
from tools.search.base import SearchBackend, generate_paper_id, RateLimiter
from tools.search.http_client import get_search_http_client
from tools.search.registry import contact_email

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_LIMITER_NO_KEY = RateLimiter(max_concurrent=1, min_interval=0.34, fast_fail_429=True)
_LIMITER_KEY = RateLimiter(max_concurrent=2, min_interval=0.11, fast_fail_429=True)


def parse_pubmed_xml(xml: str) -> list[Paper]:
    root = ET.fromstring(xml)
    papers: list[Paper] = []
    for article in root.findall(".//PubmedArticle"):
        medline = article.find("MedlineCitation")
        record = medline.find("Article") if medline is not None else None
        if record is None:
            continue
        title_node = record.find("ArticleTitle")
        title = "".join(title_node.itertext()).strip() if title_node is not None else ""
        authors = []
        for author in record.findall(".//AuthorList/Author"):
            collective = author.findtext("CollectiveName")
            name = collective or " ".join(filter(None, [author.findtext("ForeName"), author.findtext("LastName")]))
            if name.strip(): authors.append(name.strip())
        year = None
        for path in ("Journal/JournalIssue/PubDate/Year", "ArticleDate/Year"):
            raw = record.findtext(path)
            if raw and raw[:4].isdigit(): year = int(raw[:4]); break
        doi = None
        for aid in article.findall(".//ArticleIdList/ArticleId"):
            if (aid.attrib.get("IdType") or "").lower() == "doi": doi = (aid.text or "").strip().lower()
        pmid = medline.findtext("PMID") if medline is not None else ""
        abstract = " ".join("".join(node.itertext()).strip() for node in record.findall(".//Abstract/AbstractText")).strip()
        venue = record.findtext("Journal/Title") or ""
        papers.append(Paper(
            id=generate_paper_id(title, authors[0] if authors else "", year, doi), title=title,
            authors=authors, year=year, venue=venue, doi=doi, source="pubmed",
            abstract=abstract, pdf_url=None, urls={"pubmed": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"} if pmid else {},
        ))
    return papers


class PubMedBackend(SearchBackend):
    name = "pubmed"
    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        s = get_settings().search
        email = contact_email(s)
        if not email:
            return []
        common = {"db": "pubmed", "tool": "PaperAgent", "email": email}
        if s.ncbi_api_key: common["api_key"] = s.ncbi_api_key
        limiter = _LIMITER_KEY if s.ncbi_api_key else _LIMITER_NO_KEY
        client = get_search_http_client()
        try:
            async with limiter:
                resp = await limiter.fetch(client, "GET", ESEARCH_URL, params={**common, "term": query, "retmode": "json", "retmax": min(limit, 50)})
            self._capture_response(resp)
            if resp.status_code != 200: return []
            payload = resp.json()
            result = payload.get("esearchresult") if isinstance(payload, dict) else None
            ids = result.get("idlist") if isinstance(result, dict) else None
            if not isinstance(ids, list):
                self._forced_status="schema_mismatch"
                return []
            if not ids: return []
            async with limiter:
                resp = await limiter.fetch(client, "GET", EFETCH_URL, params={**common, "id": ",".join(ids), "retmode": "xml"})
            self._capture_response(resp)
            return parse_pubmed_xml(resp.text) if resp.status_code == 200 else []
        except httpx.TimeoutException:
            self._forced_status="timeout"; return []
        except httpx.RequestError:
            self._forced_status="connection_error"; return []
        except (ET.ParseError, ValueError, TypeError):
            self._forced_status="schema_mismatch"; return []
