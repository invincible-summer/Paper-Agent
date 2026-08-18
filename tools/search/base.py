"""Base class and shared utilities for search backends."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import re
import time
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


@dataclass
class SearchOutcome:
    source: str
    papers: list[Paper] = field(default_factory=list)
    status: str = "ok"
    http_status: int | None = None
    request_count: int = 0
    queue_ms: int = 0
    connect_ms: int = 0
    read_ms: int = 0
    network_ms: int = 0
    redirect_count: int = 0
    rate_limit_remaining: str | None = None
    rate_limit_reset: str | None = None
    error_code: str | None = None
    final_domain: str | None = None


def classify_search_status(
    papers: list[Paper],
    http_status: int | None,
    *,
    forced_status: str | None = None,
    content_type: str = "",
) -> tuple[str, str | None]:
    """Classify one source batch without conflating local and remote failures."""
    if forced_status:
        return forced_status, forced_status
    if http_status in {403, 429} and "text/html" in content_type.lower():
        return "bot_challenge", "bot_challenge"
    if http_status == 429:
        return "rate_limited", "rate_limited"
    if http_status is not None and 300 <= http_status < 400:
        return "unexpected_redirect", "unexpected_redirect"
    if http_status is not None and http_status >= 500:
        return "server_error", "server_error"
    if http_status is not None and http_status >= 400:
        return "http_error", f"http_{http_status}"
    return ("ok", None) if papers else ("reachable_empty", None)


class SearchBackend:
    """Base class for official API/local-index search backends."""

    name: str = "base"
    max_queries_per_turn: int = 2

    def _reset_telemetry(self) -> None:
        self._last_http_status = None
        self._last_redirect_count = 0
        self._last_rate_remaining = None
        self._last_rate_reset = None
        self._last_content_type = ""
        self._last_request_count = 0
        self._last_final_domain = None
        self._last_queue_ms = 0
        self._last_connect_ms = 0
        self._last_read_ms = 0
        self._forced_status = None

    def _capture_response(self, response) -> None:
        self._last_http_status = int(response.status_code)
        extensions = response.extensions if hasattr(response, "extensions") else {}
        self._last_request_count += max(1, int(extensions.get("paper_request_count", 1) or 1))
        try: self._last_final_domain = response.url.host
        except Exception:
            try: self._last_final_domain = response.request.url.host
            except Exception: pass
        history_count = len(getattr(response, "history", []) or [])
        if history_count == 0 and 300 <= int(response.status_code) < 400:
            history_count = 1
        self._last_redirect_count += history_count
        self._last_rate_remaining = response.headers.get("x-ratelimit-remaining") or response.headers.get("ratelimit-remaining")
        self._last_rate_reset = response.headers.get("x-ratelimit-reset") or response.headers.get("x-ratelimit-retry-after") or response.headers.get("ratelimit-reset")
        self._last_content_type = response.headers.get("content-type", "").lower()
        timing = extensions.get("paper_timing", {})
        self._last_queue_ms += int(timing.get("queue_ms", 0) or 0)
        self._last_connect_ms += int(timing.get("connect_ms", 0) or 0)
        self._last_read_ms += int(timing.get("read_ms", 0) or 0)

    async def search(self, query: str, limit: int = 20) -> list[Paper]:
        raise NotImplementedError

    async def search_many(self, queries: list[str], limit: int = 20) -> SearchOutcome:
        """Default source-level batch adapter for legacy/simple backends."""
        self._reset_telemetry()
        papers: list[Paper] = []
        started = time.monotonic()
        for query in queries[: self.max_queries_per_turn]:
            papers.extend(await self.search(query, limit))
            if getattr(self, "_forced_status", None):
                break
        http_status = getattr(self, "_last_http_status", None)
        forced = getattr(self, "_forced_status", None)
        status, error = classify_search_status(
            papers, http_status, forced_status=forced,
            content_type=getattr(self, "_last_content_type", ""),
        )
        queue_ms = getattr(self, "_last_queue_ms", 0)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return SearchOutcome(
            source=self.name, papers=papers, status=status, http_status=http_status,
            request_count=getattr(self, "_last_request_count", 0), queue_ms=queue_ms,
            connect_ms=getattr(self, "_last_connect_ms", 0), read_ms=getattr(self, "_last_read_ms", 0),
            network_ms=max(0, elapsed_ms - queue_ms),
            redirect_count=getattr(self, "_last_redirect_count", 0),
            rate_limit_remaining=getattr(self, "_last_rate_remaining", None),
            rate_limit_reset=getattr(self, "_last_rate_reset", None), error_code=error,
            final_domain=getattr(self, "_last_final_domain", None),
        )


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
        import contextvars
        self._queue_ms = contextvars.ContextVar(f"queue_ms_{id(self)}", default=0)

    async def __aenter__(self):
        queued_at = time.monotonic()
        await self._semaphore.acquire()
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                await __import__("asyncio").sleep(wait)
            self._last_request = time.monotonic()
        self._queue_ms.set(int((time.monotonic() - queued_at) * 1000))
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
        request_count = 0
        total_timing = {"connect_ms": 0, "read_ms": 0}
        base_extensions = dict(kwargs.pop("extensions", {}) or {})
        try:
            for attempt in range(attempts):
                trace_starts = {}
                timing = {"connect_ms": 0, "read_ms": 0}
                async def trace(name, info):
                    now = time.monotonic()
                    base = name.rsplit(".", 1)[0]
                    if name.endswith(".started"):
                        trace_starts[base] = now
                    elif name.endswith(".complete") and base in trace_starts:
                        elapsed = int((now - trace_starts.pop(base)) * 1000)
                        if "connect_tcp" in base or "start_tls" in base:
                            timing["connect_ms"] += elapsed
                        elif "receive_response_body" in base:
                            timing["read_ms"] += elapsed
                extensions = dict(base_extensions)
                extensions.setdefault("trace", trace)
                resp = await client.request(method, url, extensions=extensions, **kwargs)
                request_count += 1
                total_timing["connect_ms"] += timing["connect_ms"]
                total_timing["read_ms"] += timing["read_ms"]
                total_timing["queue_ms"] = self._queue_ms.get()
                if hasattr(resp, "extensions"):
                    resp.extensions["paper_timing"] = dict(total_timing)
                    resp.extensions["paper_request_count"] = request_count
                else:
                    try: setattr(resp, "extensions", {
                        "paper_timing": dict(total_timing),
                        "paper_request_count": request_count,
                    })
                    except Exception: pass
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
        return resp
