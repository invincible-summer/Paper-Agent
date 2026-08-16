"""OpenAlex citation reference enrichment (Phase 1). Real directed citation edges."""
from __future__ import annotations

import logging
import httpx
from core.config import get_settings
from core.models import Paper
from tools.search.base import RateLimiter

logger = logging.getLogger(__name__)

API_URL = "https://api.openalex.org/works"

# 5 concurrent, 0.5s min interval (same policy as openalex.py search backend).
_limiter = RateLimiter(max_concurrent=5, min_interval=0.5)


def _mailto() -> dict:
    """Polite-pool param only when a real email is configured.

    Without one, OpenAlex is queried anonymously (standard pool) — a
    placeholder identity must never be sent.
    """
    email = get_settings().search.openalex_email
    return {"mailto": email} if email else {}


async def fetch_referenced_works(dois: list[str]) -> dict[str, list[str]]:
    """For each DOI, return its referenced_works OpenAlex work ids (free, ~1 req/paper).

    Keyed by normalized DOI. Papers with no DOI are the caller's responsibility.
    """
    out: dict[str, list[str]] = {}
    if not dois:
        return out
    async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
        for doi in dois:
            ndoi = _norm_doi(doi)
            if not ndoi:
                continue
            url = f"{API_URL}/doi:{ndoi}"
            params = {"select": "id,referenced_works", **_mailto()}
            try:
                async with _limiter:
                    resp = await _limiter.fetch(client, "GET", url, params=params)
                if resp.status_code != 200:
                    logger.debug("OpenAlex refs lookup %s -> %s", ndoi, resp.status_code)
                    continue
                out[ndoi] = list(resp.json().get("referenced_works") or [])
            except Exception as e:
                logger.warning("OpenAlex refs lookup failed for %s: %s", ndoi, e)
    return out


async def resolve_openalex_ids(dois: list[str]) -> dict[str, str]:
    """Map each DOI -> its OpenAlex work id (W...), keyed by normalized DOI."""
    out: dict[str, str] = {}
    if not dois:
        return out
    async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
        for doi in dois:
            ndoi = _norm_doi(doi)
            if not ndoi:
                continue
            params = {"filter": f"doi:{ndoi}", "select": "id,doi", "per-page": 1, **_mailto()}
            try:
                async with _limiter:
                    resp = await _limiter.fetch(client, "GET", API_URL, params=params)
                if resp.status_code != 200:
                    continue
                results = resp.json().get("results") or []
                if results:
                    out[ndoi] = results[0].get("id", "")
            except Exception as e:
                logger.debug("OpenAlex id resolve failed for %s: %s", ndoi, e)
    return out


def build_citation_edges(
    papers: list[Paper],
    refs_by_doi: dict[str, list[str]],
    doi_to_openalex: dict[str, str],
) -> list[tuple[str, str]]:
    """Compute directed in-library citation edges.

    For paper B (citing) whose referenced_works include paper A's OpenAlex id,
    the edge is B -> A (B cites A). Returns (citer, cited) tuples using paper.id.
    """
    oaid_to_pid: dict[str, str] = {}
    for doi, oaid in doi_to_openalex.items():
        pid = _paper_id_for_doi(papers, doi)
        if pid and oaid:
            oaid_to_pid[oaid] = pid

    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for citer_pid, citer_doi in _iter_papers_with_doi(papers):
        refs = refs_by_doi.get(_norm_doi(citer_doi)) or []
        cited_pids = {oaid_to_pid[r] for r in refs if r in oaid_to_pid}
        for cited_pid in cited_pids:
            if cited_pid == citer_pid:
                continue
            key = (citer_pid, cited_pid)
            if key in seen:
                continue
            seen.add(key)
            edges.append(key)
    return edges


def snowball_misses(
    refs_by_doi: dict[str, list[str]],
    in_library_oaids: set[str],
    top_n: int = 10,
    min_cited_by: int = 2,
) -> list[str]:
    """OpenAlex work ids cited by >=min_cited_by papers but not in library.

    Returns up to top_n ids, most-cited first (the missed must-read list).
    """
    in_lib = set(in_library_oaids)
    counts: dict[str, int] = {}
    seen_per_doi: dict[str, set[str]] = {}
    for doi, refs in refs_by_doi.items():
        local = seen_per_doi.setdefault(doi, set())
        for r in refs:
            if r in in_lib or r in local:
                continue
            local.add(r)
            counts[r] = counts.get(r, 0) + 1
    ranked = sorted(
        (aid for aid, c in counts.items() if c >= min_cited_by),
        key=lambda aid: counts[aid],
        reverse=True,
    )
    return ranked[:top_n]


async def fetch_works_metadata(openalex_ids: list[str]) -> list[dict]:
    """Batch-fetch metadata for a list of OpenAlex work ids (1 request)."""
    if not openalex_ids:
        return []
    flt = "|".join(openalex_ids)
    params = {
        "filter": f"ids.openalex:{flt}",
        "select": "id,doi,title,publication_year,cited_by_count,authorships,primary_location",
        "per-page": min(len(openalex_ids), 50),
        **_mailto(),
    }
    async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
        try:
            async with _limiter:
                resp = await _limiter.fetch(client, "GET", API_URL, params=params)
            if resp.status_code != 200:
                return []
            return resp.json().get("results") or []
        except Exception as e:
            logger.warning("OpenAlex metadata batch fetch failed: %s", e)
            return []


def metadata_to_paper(item: dict) -> Paper:
    """Convert an OpenAlex metadata record into a minimal Paper (reference layer)."""
    authors = [
        a["author"]["display_name"]
        for a in item.get("authorships", [])
        if a.get("author", {}).get("display_name")
    ]
    doi_raw = item.get("doi") or ""
    doi = doi_raw.replace("https://doi.org/", "") if doi_raw else None
    venue = ""
    loc = item.get("primary_location") or {}
    src = loc.get("source") or {}
    if src:
        venue = src.get("display_name", "")
    return Paper(
        id=item.get("id", ""),
        title=item.get("title") or item.get("display_name") or "",
        authors=authors,
        year=item.get("publication_year"),
        venue=venue,
        doi=doi,
        source="openalex",
        citation_count=item.get("cited_by_count", 0),
        urls={"openalex": item.get("id", "")},
    )


def in_library_openalex_ids(doi_to_openalex: dict[str, str], papers: list[Paper]) -> set[str]:
    """Set of OpenAlex ids for in-library papers that have a resolved DOI."""
    ids: set[str] = set()
    for doi, oaid in doi_to_openalex.items():
        if _paper_id_for_doi(papers, doi) and oaid:
            ids.add(oaid)
    return ids


def _norm_doi(doi: str | None) -> str:
    if not doi:
        return ""
    return doi.strip().replace("https://doi.org/", "").lower()


def _paper_id_for_doi(papers: list[Paper], doi: str) -> str | None:
    ndoi = _norm_doi(doi)
    if not ndoi:
        return None
    for p in papers:
        if _norm_doi(p.doi) == ndoi:
            return p.id
    return None


def _iter_papers_with_doi(papers: list[Paper]):
    for p in papers:
        if p.doi:
            yield p.id, p.doi
