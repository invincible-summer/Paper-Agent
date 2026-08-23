"""OpenAIRE Graph API V3 research-product search backend.

The request, OAuth2 client-credentials flow, anonymous allowance and response
shape are kept aligned with OpenAIRE's official Graph API documentation.  The
Graph response is metadata/abstract-only here: repository instance URLs are
not parsed as document candidates and never enter a download path.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

import httpx

from core.config import get_settings
from core.models import Paper
from tools.search.base import (
    RateLimiter,
    SearchBackend,
    generate_paper_id,
    shorten_chinese_query,
)
from tools.search.http_client import get_search_http_client

API_URL = "https://api.openaire.eu/graph/v3/research-products"
TOKEN_URL = "https://aai.openaire.eu/oidc/token"
ANONYMOUS_OFFICIAL_CALLS_PER_HOUR = 60
# Leave ten requests of safety headroom for another process/operator using the
# same public IP while still remaining strictly inside the official allowance.
ANONYMOUS_LOCAL_CALLS_PER_HOUR = 50
AUTHENTICATED_OFFICIAL_CALLS_PER_HOUR = 7200

_limiter = RateLimiter(max_concurrent=2, min_interval=1.0, fast_fail_429=True)
_token: tuple[str, float] | None = None
_anonymous_calls: deque[float] = deque()


def _allow_anonymous() -> bool:
    now = time.time()
    while _anonymous_calls and now - _anonymous_calls[0] >= 3600:
        _anonymous_calls.popleft()
    if len(_anonymous_calls) >= ANONYMOUS_LOCAL_CALLS_PER_HOUR:
        return False
    _anonymous_calls.append(now)
    return True


def _first(value: Any) -> str:
    """Return the first non-empty scalar from documented nested variants."""
    if isinstance(value, list):
        for item in value:
            text = _first(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("$", "value", "name", "fullName", "title", "id"):
            text = _first(value.get(key))
            if text:
                return text
        return ""
    return str(value or "").strip()


def _doi(item: dict) -> str | None:
    for pid in item.get("pids") or item.get("identifiers") or []:
        if not isinstance(pid, dict):
            continue
        scheme = str(pid.get("scheme") or pid.get("type") or "").lower()
        if scheme == "doi":
            return _first(pid).lower() or None
    return None


def _valid_response_schema(data: object) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("header"), dict)
        and isinstance(data.get("results"), list)
    )


def parse_results(data: dict) -> list[Paper]:
    """Parse the official Graph API V3 ``header`` + ``results`` shape."""
    papers: list[Paper] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = _first(item.get("mainTitle") or item.get("title"))
        openaire_id = _first(item.get("id"))
        if not title or not openaire_id:
            continue
        authors = [
            name
            for author in item.get("authors") or item.get("creators") or []
            if (name := _first(author))
        ]
        date = _first(
            item.get("publicationDate")
            or item.get("dateOfAcceptance")
            or item.get("date")
        )
        year = int(date[:4]) if date[:4].isdigit() else None
        doi = _doi(item)
        papers.append(Paper(
            id=generate_paper_id(
                title, authors[0] if authors else "", year, doi
            ),
            title=title,
            authors=authors,
            year=year,
            venue=_first(item.get("publisher") or item.get("journal")),
            doi=doi,
            source="openaire",
            abstract=_first(item.get("description") or item.get("descriptions")),
            urls={
                "openaire": (
                    "https://explore.openaire.eu/search/publication?pid="
                    f"{openaire_id}"
                )
            },
        ))
    return papers


async def _access_token() -> str:
    """Obtain and cache the documented OAuth2 client-credentials token."""
    global _token
    settings = get_settings().search
    client_id = getattr(settings, "openaire_client_id", "") or ""
    client_secret = getattr(settings, "openaire_client_secret", "") or ""
    if not client_id or not client_secret:
        return ""
    if _token and _token[1] > time.time() + 60:
        return _token[0]
    try:
        response = await get_search_http_client().post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
        )
        if response.status_code != 200:
            return ""
        data = response.json()
        if not isinstance(data, dict):
            return ""
        token = str(data.get("access_token") or "")
        ttl = max(60, int(data.get("expires_in") or 300))
    except (httpx.HTTPError, TypeError, ValueError):
        return ""
    if token:
        _token = (token, time.time() + ttl)
    return token


class OpenAireBackend(SearchBackend):
    name = "openaire"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        params = {
            "search": shorten_chinese_query(query),
            "type": "publication",
            "page": 1,
            "pageSize": min(limit, 50),
        }
        try:
            token = await _access_token()
            headers = {"Authorization": f"Bearer {token}"} if token else None
            if not token and not _allow_anonymous():
                self._forced_status = "local_budget_exhausted"
                return []
            async with _limiter:
                response = await _limiter.fetch(
                    get_search_http_client(),
                    "GET",
                    API_URL,
                    params=params,
                    headers=headers,
                )
            self._capture_response(response)
            if response.status_code != 200:
                return []
            data = response.json()
            if not _valid_response_schema(data):
                self._forced_status = "schema_mismatch"
                return []
            return parse_results(data)
        except httpx.TimeoutException:
            self._forced_status = "timeout"
            return []
        except httpx.RequestError:
            self._forced_status = "connection_error"
            return []
        except (ValueError, TypeError):
            self._forced_status = "schema_mismatch"
            return []
