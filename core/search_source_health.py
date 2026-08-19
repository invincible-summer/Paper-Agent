"""Single-worker, process-local health and circuit state for paper sources."""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass


@dataclass
class SourceHealth:
    source: str
    state: str = "closed"
    consecutive_failures: int = 0
    open_until: float = 0.0
    last_http_status: int | None = None
    last_latency_ms: int | None = None
    last_error_code: str | None = None
    last_checked_at: float | None = None


class SearchSourceHealthRegistry:
    def __init__(self, threshold: int = 3, cooldown_seconds: float = 300.0):
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._states: dict[str, SourceHealth] = {}
        self._lock = threading.Lock()
        self._half_open_active: set[str] = set()

    def _get(self, source: str) -> SourceHealth:
        return self._states.setdefault(source, SourceHealth(source=source))

    def allow(self, source: str) -> tuple[bool, str, float]:
        with self._lock:
            item = self._get(source)
            now = time.time()
            if item.state == "open" and now < item.open_until:
                return False, "open", max(0.0, item.open_until - now)
            if item.state == "open":
                item.state = "half_open"
                if source in self._half_open_active:
                    return False, "half_open", 0.0
                self._half_open_active.add(source)
                return True, "half_open", 0.0
            if item.state == "half_open" and source in self._half_open_active:
                return False, "half_open", 0.0
            return True, item.state, 0.0

    def record_success(self, source: str, *, status: int | None = None,
                       latency_ms: int | None = None) -> None:
        with self._lock:
            item = self._get(source)
            item.state = "closed"
            item.consecutive_failures = 0
            item.open_until = 0.0
            item.last_http_status = status
            item.last_latency_ms = latency_ms
            item.last_error_code = None
            item.last_checked_at = time.time()
            self._half_open_active.discard(source)

    def record_failure(self, source: str, code: str, *, status: int | None = None,
                       latency_ms: int | None = None) -> None:
        with self._lock:
            item = self._get(source)
            item.consecutive_failures += 1
            item.last_http_status = status
            item.last_latency_ms = latency_ms
            item.last_error_code = code
            item.last_checked_at = time.time()
            self._half_open_active.discard(source)
            if item.state == "half_open" or item.consecutive_failures >= self.threshold:
                item.state = "open"
                item.open_until = time.time() + self.cooldown_seconds

    def note(self, source: str, *, status: int | None, latency_ms: int | None,
             error_code: str | None) -> None:
        if error_code in {"rate_limited", "timeout", "connection_error", "server_error", "invalid_response"}:
            self.record_failure(source, error_code, status=status, latency_ms=latency_ms)
        elif status is not None and 200 <= status < 300:
            self.record_success(source, status=status, latency_ms=latency_ms)
        else:
            with self._lock:
                item = self._get(source)
                # A non-breaker response must release a half-open trial; keep
                # the HTTP diagnostic without turning a 4xx/config issue into a
                # connectivity circuit failure.
                if item.state == "half_open":
                    item.state = "closed"
                    item.open_until = 0.0
                    self._half_open_active.discard(source)
                item.last_http_status = status
                item.last_latency_ms = latency_ms
                item.last_error_code = error_code or ("client_error" if status and status >= 400 else None)
                item.last_checked_at = time.time()

    def get(self, source: str) -> dict:
        with self._lock:
            item = self._get(source)
            row = asdict(item)
            row["remaining_seconds"] = max(0.0, item.open_until - time.time()) if item.state == "open" else 0.0
            return row

    def snapshot(self) -> list[dict]:
        with self._lock:
            now = time.time()
            rows = []
            for source, item in sorted(self._states.items()):
                row = asdict(item)
                row["remaining_seconds"] = max(0.0, item.open_until - now) if item.state == "open" else 0.0
                rows.append(row)
            return rows

    def reset(self) -> None:
        with self._lock:
            self._states.clear()
            self._half_open_active.clear()

    def force_open(self, source: str, seconds: float | None = None) -> None:
        """Administrative trip: block this source for ``seconds`` (default
        one cooldown window). Unlike organic failures this keeps
        ``consecutive_failures`` untouched so the source recovers cleanly
        after the window (or an explicit :meth:`force_close`)."""
        with self._lock:
            item = self._get(source)
            item.state = "open"
            item.open_until = time.time() + (
                self.cooldown_seconds if seconds is None else max(1.0, float(seconds)))
            item.last_checked_at = time.time()
            self._half_open_active.discard(source)

    def force_close(self, source: str) -> None:
        """Administrative recovery: clear failures and any open window."""
        with self._lock:
            item = self._get(source)
            item.state = "closed"
            item.consecutive_failures = 0
            item.open_until = 0.0
            item.last_checked_at = time.time()
            self._half_open_active.discard(source)


GLOBAL_SEARCH_HEALTH = SearchSourceHealthRegistry()


def get_search_health_registry() -> SearchSourceHealthRegistry:
    return GLOBAL_SEARCH_HEALTH
