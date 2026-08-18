"""Base class and shared utilities for search backends."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from core.models import Paper


def is_chinese(query: str) -> bool:
    """Check if a query is primarily Chinese text."""
    chinese_chars = sum(1 for c in query if '\u4e00' <= c <= '\u9fff')
    return chinese_chars > len(query) * 0.3


def shorten_chinese_query(query: str, max_terms: int = 3) -> str:
    """Shorten a Chinese query to key terms for better API matching.

    OpenAlex and other English-centric databases handle long Chinese queries poorly,
    often matching individual characters and returning irrelevant results.
    """
    if not is_chinese(query):
        return query
    terms = query.split()
    if len(terms) <= max_terms:
        return query
    return " ".join(terms[:max_terms])


def normalize_title(title: str) -> str:
    """Normalize a paper title for dedup matching (DESIGN 12.5 Step 2)."""
    title = unicodedata.normalize("NFKC", title.lower().strip())
    title = re.sub(r"[^\w\s]", "", title)      # remove punctuation
    title = re.sub(r"\s+", " ", title)          # collapse whitespace
    return title


def title_similarity(a: str, b: str) -> float:
    """Quick similarity based on token overlap (Jaccard)."""
    ta = set(normalize_title(a).split())
    tb = set(normalize_title(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def generate_paper_id(title: str, first_author: str, year: int | None, doi: str | None) -> str:
    """Generate a unique paper ID (DESIGN D-006).

    - Has DOI: 'doi:<doi>'
    - No DOI: 'hash:<sha256 of normalized title + first author + year>'
    """
    if doi:
        return f"doi:{doi}"
    normalized = normalize_title(title)
    raw = f"{normalized}|{first_author}|{year or ''}"
    return f"hash:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


class SearchBackend:
    """Base class for all search backends."""

    name: str = "base"

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        raise NotImplementedError


class RateLimiter:
    """Per-source concurrency limiter + minimum request interval + 429 retry.

    Usage:
        limiter = RateLimiter(max_concurrent=5, min_interval=0.5)
        async with limiter:
            resp = await limiter.fetch(client, method, url, **kwargs)
    """

    def __init__(self, max_concurrent: int = 5, min_interval: float = 0.5,
                 *, source_name: str | None = None, fast_fail_429: bool = False):
        self._semaphore = __import__("asyncio").Semaphore(max_concurrent)
        self._min_interval = min_interval
        self._last_request = 0.0
        self._lock = __import__("asyncio").Lock()
        self._source_name = source_name
        self._fast_fail_429 = fast_fail_429

    async def __aenter__(self):
        await self._semaphore.acquire()
        async with self._lock:
            import time
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                await __import__("asyncio").sleep(wait)
            self._last_request = time.monotonic()
        return self

    async def __aexit__(self, *args):
        self._semaphore.release()

    async def fetch(self, client, method: str, url: str, max_retries: int = 3, **kwargs):
        """Fetch with bounded 429 retry and process-local source telemetry.

        Main paper-search backends opt into ``fast_fail_429``: at most one
        retry and two seconds of waiting, so a cloud-IP 429 cannot consume the
        whole chat turn. Other utility callers retain the legacy budget.
        """
        import asyncio
        import time

        attempts = min(max_retries, 2) if self._fast_fail_429 else max_retries
        wait_cap = 2.0 if self._fast_fail_429 else 30.0
        started = time.monotonic()
        resp = None
        try:
            for attempt in range(attempts):
                resp = await client.request(method, url, **kwargs)
                if resp.status_code == 429 and attempt + 1 < attempts:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait = float(retry_after) if retry_after else 2 ** attempt
                    except ValueError:
                        wait = 2 ** attempt
                    await asyncio.sleep(min(wait, wait_cap))
                    continue
                break
        except Exception:
            # The backend classifies timeout/request/parse failures so a single
            # network exception is counted exactly once by the source breaker.
            raise
        if self._source_name and resp is not None:
            from core.search_source_health import get_search_health_registry
            status = int(resp.status_code)
            code = ("rate_limited" if status == 429 else
                    "server_error" if status >= 500 else None)
            get_search_health_registry().note(
                self._source_name, status=status,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=code,
            )
        return resp
