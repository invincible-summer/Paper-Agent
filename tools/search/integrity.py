"""Integrity sweep — retraction / erratum / preprint→published checks.

Deterministic official-API queries (zero LLM), reusing the per-source
RateLimiter + mailto discipline of the search backends. Each paper is graded:

  ✅ clean            — no retraction / erratum found (DOI checked)
  ⚠️ concern          — Crossref relation flags a correction / expression of concern
  ⛔ retracted        — OpenAlex is_retracted == True (or Crossref has-retraction)
  🔁 preprint_published — arXiv preprint already carries a formal DOI / journal_ref
  ❓ unknown          — no DOI, or every lookup failed (honest degrade, NOT an error)

Relation semantics: Crossref `has-correction` / `has-expression-of-concern` /
`has-retraction` on THIS paper means it is the SUBJECT of such a notice
(i.e. it got corrected / retracted). `is-retraction-of` means this paper IS
the retraction notice — not a concern for citing it.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from core.config import get_settings
from core.models import Paper
from tools.search.base import RateLimiter
from tools.search.http_client import get_search_http_client

logger = logging.getLogger(__name__)

_OPENALEX = "https://api.openalex.org/works"
_CROSSREF = "https://api.crossref.org/works/"
_ARXIV = "https://export.arxiv.org/api/query"

# Same per-source policy as the search backends (DESIGN §5).
_openalex_limiter = RateLimiter(max_concurrent=5, min_interval=0.5)
_crossref_limiter = RateLimiter(max_concurrent=3, min_interval=0.5)
# arXiv ToU: single connection, 1 request / 3s — cap lookups so a sweep never stalls.
_arxiv_limiter = RateLimiter(max_concurrent=1, min_interval=3.0)
_MAX_ARXIV_LOOKUPS = 5



def _capability_allowed(source: str) -> bool:
    from core.paper_search_settings_store import source_capability_enabled
    return source_capability_enabled(source, "search")[0]

def _norm_doi(doi: str | None) -> str:
    if not doi:
        return ""
    return doi.strip().replace("https://doi.org/", "").lower()


def _openalex_auth() -> dict:
    key = getattr(get_settings().search, "openalex_api_key", "")
    return {"api_key": key} if key else {}


# ---------------------------------------------------------------------------
# Response parsers (pure, unit-tested)
# ---------------------------------------------------------------------------

def parse_openalex_integrity(work: dict) -> dict:
    """Extract retraction / paratext / merge flags from an OpenAlex work record."""
    return {
        "is_retracted": bool(work.get("is_retracted")),
        "is_paratext": bool(work.get("is_paratext")),
        "merged_into": (work.get("merged_into") or {}).get("id") or "",
    }


# Crossref relation keys meaning THIS paper is the subject of an integrity notice.
_CROSSREF_CONCERN_RELATIONS = (
    "has-retraction", "has-correction", "has-expression-of-concern",
)


def parse_crossref_relations(message: dict) -> dict:
    """Read Crossref relation/assertion for corrections & retractions.

    Returns concerns (human-readable list) + is_retraction (subject of a retraction).
    """
    rel = message.get("relation") or {}
    concerns: list[str] = []
    is_retraction = False
    for reltype in _CROSSREF_CONCERN_RELATIONS:
        entries = rel.get(reltype) or []
        if not entries:
            continue
        if reltype == "has-retraction":
            is_retraction = True
        for entry in entries:
            ident = entry.get("id", "") if isinstance(entry, dict) else str(entry)
            concerns.append(f"{reltype}: {ident}".rstrip(": "))
    # Some publishers encode the retraction as a structured assertion.
    for a in (message.get("assertion") or []):
        if not isinstance(a, dict):
            continue
        value = str(a.get("value", "")).lower()
        if "retract" in value:
            is_retraction = True
            label = a.get("label") or a.get("name") or "retraction-assertion"
            concerns.append(f"assertion: {label}")
    return {"concerns": concerns, "is_retraction": is_retraction}


def _arxiv_id(paper: Paper) -> str:
    """Extract the arXiv id (e.g. 2401.12345v1) from a paper's arxiv url."""
    url = (paper.urls or {}).get("arxiv", "")
    for sep in ("/abs/", "/pdf/"):
        if sep in url:
            tail = url.split(sep, 1)[1]
            return tail[:-4] if tail.lower().endswith(".pdf") else tail
    return ""


# ---------------------------------------------------------------------------
# Lookups (best-effort, rate-limited; None = lookup failed)
# ---------------------------------------------------------------------------

async def _openalex_lookup(client: httpx.AsyncClient, doi: str) -> dict | None:
    if not _capability_allowed("openalex") or not getattr(get_settings().search, "openalex_api_key", ""):
        return None
    url = f"{_OPENALEX}/doi:{doi}"
    params = {"select": "id,is_retracted,is_paratext,merged_into", **_openalex_auth()}
    try:
        async with _openalex_limiter:
            resp = await _openalex_limiter.fetch(client, "GET", url, params=params)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:  # noqa: BLE001
        logger.debug("OpenAlex integrity lookup %s failed: %s", doi, e)
        return None


async def _crossref_lookup(client: httpx.AsyncClient, doi: str) -> dict | None:
    if not _capability_allowed("crossref"):
        return None
    from tools.search.registry import contact_email
    email = contact_email(get_settings().search)
    params = {"mailto": email} if email else None
    try:
        async with _crossref_limiter:
            resp = await _crossref_limiter.fetch(
                client, "GET", _CROSSREF + quote(doi, safe=""), params=params)
        if resp.status_code != 200:
            return None
        return resp.json().get("message") or {}
    except Exception as e:  # noqa: BLE001
        logger.debug("Crossref integrity lookup %s failed: %s", doi, e)
        return None


async def _arxiv_journal_ref(client: httpx.AsyncClient, arxiv_id: str) -> str:
    """Best-effort: read arXiv journal_ref for a preprint id (1 feedparser call)."""
    if not _capability_allowed("arxiv"):
        return ""
    import feedparser
    try:
        async with _arxiv_limiter:
            resp = await _arxiv_limiter.fetch(
                client, "GET", _ARXIV, params={"id_list": arxiv_id})
        if resp.status_code != 200:
            return ""
        feed = feedparser.parse(resp.text)
        for entry in feed.entries:
            return (entry.get("arxiv_journal_ref") or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("arXiv journal_ref lookup %s failed: %s", arxiv_id, e)
    return ""


# ---------------------------------------------------------------------------
# Per-paper check
# ---------------------------------------------------------------------------

async def _check_one(client: httpx.AsyncClient, paper: Paper,
                     arxiv_lookup_ids: set[str]) -> dict:
    base = {"paper_id": paper.id, "title": paper.title,
            "doi": paper.doi, "source": paper.source}
    doi = _norm_doi(paper.doi)
    checked = False
    retracted = False
    concerns: list[str] = []

    if doi:
        oa = await _openalex_lookup(client, doi)
        if oa is not None:
            checked = True
            if parse_openalex_integrity(oa)["is_retracted"]:
                retracted = True
        cr = await _crossref_lookup(client, doi)
        if cr is not None:
            checked = True
            ci = parse_crossref_relations(cr)
            concerns.extend(ci["concerns"])
            if ci["is_retraction"]:
                retracted = True

    # Preprint → published version (arXiv only)
    preprint_published = ""
    if paper.source == "arxiv":
        if doi:
            preprint_published = f"已有正式 DOI {doi}，建议改引正式版"
        elif _arxiv_id(paper) in arxiv_lookup_ids:
            jr = await _arxiv_journal_ref(client, _arxiv_id(paper))
            if jr:
                preprint_published = f"arXiv journal_ref：{jr}，建议改引正式版"

    if retracted:
        status, note = "retracted", "⛔ 已被撤稿，不应引用"
        if concerns:
            note += "（" + "；".join(concerns) + "）"
    elif concerns:
        status, note = "concern", "⚠️ 存在勘误或关切声明：" + "；".join(concerns)
    elif preprint_published:
        status, note = "preprint_published", "🔁 预印本已有正式版：" + preprint_published
    elif doi and checked:
        status, note = "clean", "✅ 未发现撤稿或勘误"
    else:
        status, note = "unknown", "❓ 未能查到（无 DOI 或查询失败），请人工核对"
    return {**base, "status": status, "note": note}


def _group_facts(report: list[dict]) -> dict:
    """Group paper ids by status; drop empty buckets for compactness."""
    buckets: dict[str, list[str]] = {}
    for r in report:
        buckets.setdefault(r["status"], []).append(r["paper_id"])
    return buckets


async def sweep(papers: list[Paper]) -> dict:
    """Run integrity checks across papers concurrently. Returns {report, facts}.

    Each paper → one report row {paper_id, title, doi, source, status, note}.
    """
    import asyncio

    # Cap arXiv journal_ref lookups (3s each) so a large candidate set can't stall.
    arxiv_lookup_ids = {
        _arxiv_id(p) for p in papers
        if p.source == "arxiv" and not _norm_doi(p.doi) and _arxiv_id(p)
    }
    arxiv_lookup_ids = set(list(arxiv_lookup_ids)[:_MAX_ARXIV_LOOKUPS])

    client = get_search_http_client()
    tasks = [_check_one(client, p, arxiv_lookup_ids) for p in papers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    report: list[dict] = []
    for p, res in zip(papers, results):
        if isinstance(res, Exception):
            report.append({"paper_id": p.id, "title": p.title, "doi": p.doi,
                           "source": p.source, "status": "unknown",
                           "note": f"❓ 查询失败：{res}，请人工核对"})
        else:
            report.append(res)
    return {"report": report, "facts": _group_facts(report)}
