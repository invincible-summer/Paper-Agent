"""Crossref metadata enrichment for citation export (by DOI, best-effort).

Search results often lack volume/issue/pages/publisher. For citation export
we fill them from Crossref `GET /works/{doi}` — same polite-pool discipline
as the search backend (mailto when configured, anonymous otherwise), capped
and failure-tolerant: papers without a DOI or with a failed lookup export
with whatever metadata they already have.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from core.config import get_settings
from tools.search.base import RateLimiter

logger = logging.getLogger(__name__)

_API = "https://api.crossref.org/works/"
_limiter = RateLimiter(max_concurrent=3, min_interval=0.5)
_MAX_LOOKUPS = 20  # per export call


async def enrich_by_dois(dois: list[str]) -> dict[str, dict]:
    """doi -> {volume?, issue?, page?, publisher?, venue?}; failures omitted."""
    dois = [d for d in dict.fromkeys(dois) if d][:_MAX_LOOKUPS]
    if not dois:
        return {}
    s = get_settings()
    email = s.search.crossref_email or s.search.openalex_email
    params = {"mailto": email} if email else None
    out: dict[str, dict] = {}
    try:
        async with httpx.AsyncClient(timeout=20, proxy=None, trust_env=False) as client:
            for doi in dois:
                try:
                    async with _limiter:
                        resp = await _limiter.fetch(
                            client, "GET", _API + quote(doi, safe=""), params=params)
                    if resp.status_code != 200:
                        continue
                    m = resp.json().get("message", {})
                except Exception:  # noqa: BLE001
                    continue
                rec: dict[str, str] = {}
                for src, dst in (("volume", "volume"), ("issue", "issue"),
                                 ("page", "page"), ("publisher", "publisher")):
                    v = m.get(src)
                    if v:
                        rec[dst] = str(v)
                venue = (m.get("container-title") or [""])[0]
                if venue:
                    rec["venue"] = venue
                if rec:
                    out[doi] = rec
    except Exception as e:  # noqa: BLE001
        logger.debug("crossref enrichment aborted: %s", e)
    return out
