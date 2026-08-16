"""OpenAIRE search backend - EU open-access research graph, free REST API, no key.
Aggregates OA publications from repositories/publishers worldwide; fulltext URLs
are only taken from instances explicitly marked Open Access.
"""
from __future__ import annotations

import httpx

from core.models import Paper
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter

API_URL = "https://api.openaire.eu/search/publications"

_limiter = RateLimiter(max_concurrent=3, min_interval=0.5)


def _val(node) -> str:
    """OpenAIRE JSON wraps scalars as {"$": text}; accept str/list/dict."""
    if node is None:
        return ""
    if isinstance(node, dict):
        return str(node.get("$") or node.get("@name") or "")
    if isinstance(node, list):
        return _val(node[0]) if node else ""
    return str(node)


def _is_open_access(instance: dict) -> bool:
    ar = instance.get("accessright") or {}
    text = f"{ar.get('@classid', '')} {ar.get('@classname', '')}".lower()
    return "open" in text or "c_abf2" in text  # COAR open-access vocabulary


def _extract_oa_url(result: dict) -> str | None:
    """Fulltext URL from Open Access instances only (never closed/embargoed)."""
    children = result.get("children") or {}
    instances = children.get("instance") or []
    if isinstance(instances, dict):
        instances = [instances]
    for inst in instances:
        if not isinstance(inst, dict) or not _is_open_access(inst):
            continue
        url = _val((inst.get("webresource") or {}).get("url"))
        if url.startswith(("http://", "https://")):
            return url
    return None


def _extract_doi(result: dict) -> str | None:
    pids = result.get("pid") or []
    if isinstance(pids, dict):
        pids = [pids]
    for pid in pids:
        if isinstance(pid, dict) and (pid.get("@classid") or "").lower() == "doi":
            doi = _val(pid)
            if doi:
                return doi
    return None


def parse_results(data: dict) -> list[Paper]:
    """Parse an OpenAIRE JSON response into Papers (pure, offline-testable)."""
    papers: list[Paper] = []
    results = ((data.get("response") or {}).get("results") or {}).get("result", []) or []
    for item in results:
        result = ((item.get("metadata") or {}).get("oaf:entity") or {}).get("oaf:result") or {}
        title = _val(result.get("title"))
        if not title:
            continue
        creators = result.get("creator") or []
        if isinstance(creators, dict):
            creators = [creators]
        authors = [_val(c) for c in creators if _val(c)]
        year = None
        date = _val(result.get("dateofacceptance"))
        if date[:4].isdigit():
            year = int(date[:4])
        doi = _extract_doi(result)
        venue = _val((result.get("journal") or {}).get("title")) or _val(result.get("journal"))
        papers.append(Paper(
            id=generate_paper_id(title, authors[0] if authors else "", year, doi),
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            source="openaire",
            language="en",
            citation_count=0,
            abstract=_val(result.get("description")),
            pdf_url=_extract_oa_url(result),
            keywords=[],
            urls={},
        ))
    return papers


class OpenAireBackend(SearchBackend):
    name = "openaire"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        query = shorten_chinese_query(query)
        params = {"format": "json", "size": min(limit, 50), "keywords": query}
        try:
            async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
                async with _limiter:
                    resp = await _limiter.fetch(client, "GET", API_URL, params=params)
                if resp.status_code != 200:
                    return []
                data = resp.json()
        except Exception:
            return []
        return parse_results(data)
