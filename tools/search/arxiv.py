"""arXiv official Atom API backend with source-level compliant batching."""
from __future__ import annotations

import re
import time

import feedparser
import httpx

from core.models import Paper
from tools.search.base import (
    SearchBackend,
    SearchOutcome,
    classify_search_status,
    generate_paper_id,
    is_chinese,
    RateLimiter,
)
from tools.search.http_client import get_search_http_client

API_URL = "https://export.arxiv.org/api/query"
# The official arXiv API manual asks clients to wait at least three seconds
# between repeated calls. Keep these constants testable and use one caller.
ARXIV_MAX_CONCURRENT = 1
ARXIV_MIN_INTERVAL_SECONDS = 3.0
_limiter = RateLimiter(
    max_concurrent=ARXIV_MAX_CONCURRENT,
    min_interval=ARXIV_MIN_INTERVAL_SECONDS,
    source_name="arxiv",
    fast_fail_429=True,
)


def _quote_query(query: str) -> str:
    clean = re.sub(r"[\x00-\x1f]+", " ", query).replace("\\", " ").replace('"', " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return f'all:"{clean[:240]}"'


def _parse_feed(xml: str) -> list[Paper]:
    feed = feedparser.parse(xml)
    if getattr(feed, "bozo", False) and not feed.entries:
        raise ValueError("invalid_atom")
    papers: list[Paper] = []
    for entry in feed.entries:
        title = entry.get("title", "").strip().replace("\n", " ")
        if not title: continue
        authors = [a.get("name", "") for a in entry.get("authors", []) if a.get("name")]
        year = int(entry["published"][:4]) if entry.get("published", "")[:4].isdigit() else None
        doi = None; pdf_url = None
        for link in entry.get("links", []):
            href = link.get("href", "")
            if "doi.org" in href: doi = href.split("doi.org/", 1)[-1]
            if link.get("title") == "pdf": pdf_url = href
        first_author = authors[0] if authors else ""
        papers.append(Paper(
            id=generate_paper_id(title, first_author, year, doi), title=title, authors=authors,
            year=year, venue="arXiv", doi=doi, source="arxiv", language="en",
            abstract=entry.get("summary", "").strip().replace("\n", " "), pdf_url=pdf_url,
            keywords=[t.get("term", "") for t in entry.get("tags", [])[:5] if t.get("term")],
            urls={"arxiv": entry.get("id", "")},
        ))
    return papers


class ArxivBackend(SearchBackend):
    name = "arxiv"
    max_queries_per_turn = 2

    async def _request(self, search_query: str, limit: int) -> tuple[list[Paper], int]:
        params = {"search_query": search_query, "start": 0, "max_results": min(limit, 50), "sortBy": "relevance"}
        async with _limiter:
            response = await _limiter.fetch(get_search_http_client(), "GET", API_URL, params=params)
        self._capture_response(response)
        if response.status_code != 200:
            return [], response.status_code
        return _parse_feed(response.text), response.status_code

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        if is_chinese(query): return []
        try: return (await self._request(_quote_query(query), limit))[0]
        except (httpx.HTTPError, ValueError): return []

    async def search_many(self, queries: list[str], limit: int = 20) -> SearchOutcome:
        self._reset_telemetry()
        english = [q for q in queries if q.strip() and not is_chinese(q)][:8]
        if not english:
            return SearchOutcome(self.name, [], "unsupported_language")
        chunks = [english[:4], english[4:8]]
        papers: list[Paper] = []; status = 200; batch_requests = 0; started = time.monotonic()
        try:
            for chunk in chunks:
                if not chunk: continue
                batch_requests += 1
                found, status = await self._request(" OR ".join(_quote_query(q) for q in chunk), limit)
                papers.extend(found)
                if status != 200: break
            outcome_status, error = classify_search_status(
                papers, status, content_type=getattr(self, "_last_content_type", ""),
            )
            queue_ms = getattr(self, "_last_queue_ms", 0)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return SearchOutcome(self.name, papers, outcome_status,
                                 status, max(batch_requests, getattr(self, "_last_request_count", 0)), queue_ms=queue_ms,
                                 connect_ms=getattr(self,"_last_connect_ms",0), read_ms=getattr(self,"_last_read_ms",0),
                                 network_ms=max(0, elapsed_ms - queue_ms),
                                 redirect_count=getattr(self,"_last_redirect_count",0),
                                 rate_limit_remaining=getattr(self,"_last_rate_remaining",None),
                                 rate_limit_reset=getattr(self,"_last_rate_reset",None), error_code=error,
                                 final_domain=getattr(self, "_last_final_domain", None))
        except httpx.TimeoutException:
            queue_ms = getattr(self, "_last_queue_ms", 0)
            return SearchOutcome(self.name, papers, "timeout", request_count=getattr(self, "_last_request_count", 0),
                                 queue_ms=queue_ms, network_ms=max(0, int((time.monotonic()-started)*1000)-queue_ms),
                                 error_code="timeout", final_domain=getattr(self, "_last_final_domain", None))
        except httpx.RequestError:
            queue_ms = getattr(self, "_last_queue_ms", 0)
            return SearchOutcome(self.name, papers, "connection_error", request_count=getattr(self, "_last_request_count", 0),
                                 queue_ms=queue_ms, network_ms=max(0, int((time.monotonic()-started)*1000)-queue_ms),
                                 error_code="connection_error", final_domain=getattr(self, "_last_final_domain", None))
        except ValueError:
            queue_ms = getattr(self, "_last_queue_ms", 0)
            return SearchOutcome(self.name, papers, "schema_mismatch", request_count=getattr(self, "_last_request_count", 0),
                                 queue_ms=queue_ms, network_ms=max(0, int((time.monotonic()-started)*1000)-queue_ms),
                                 error_code="schema_mismatch", final_domain=getattr(self, "_last_final_domain", None))
