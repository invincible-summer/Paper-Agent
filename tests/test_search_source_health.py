from __future__ import annotations

import asyncio

from core.search_source_health import SearchSourceHealthRegistry
from tools.search.base import RateLimiter


def test_breaker_opens_half_opens_and_recovers(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("core.search_source_health.time.time", lambda: now[0])
    health = SearchSourceHealthRegistry(threshold=3, cooldown_seconds=10)
    for _ in range(3):
        health.record_failure("openalex", "rate_limited", status=429)
    allowed, state, remaining = health.allow("openalex")
    assert not allowed and state == "open" and remaining == 10
    now[0] = 111
    allowed, state, _ = health.allow("openalex")
    assert allowed and state == "half_open"
    assert health.allow("openalex")[0] is False
    health.record_success("openalex", status=200, latency_ms=12)
    assert health.allow("openalex")[0] is True
    assert health.get("openalex")["consecutive_failures"] == 0


def test_fast_429_retry_wait_is_capped(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(value):
        waits.append(value)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class Response:
        status_code = 429
        headers = {"Retry-After": "90"}

    class Client:
        calls = 0
        async def request(self, *_a, **_k):
            self.calls += 1
            return Response()

    async def run():
        client = Client()
        limiter = RateLimiter(source_name="test", fast_fail_429=True,
                              max_concurrent=1, min_interval=0)
        response = await limiter.fetch(client, "GET", "https://example.test")
        assert response.status_code == 429
        assert client.calls == 2

    asyncio.run(run())
    assert waits == [2.0]
