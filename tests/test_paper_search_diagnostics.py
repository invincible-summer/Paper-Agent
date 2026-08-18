from __future__ import annotations

import asyncio

import pytest

from core.models import Paper
from tools.search import diagnostics


def test_connectivity_returns_search_and_probe_metrics(monkeypatch):
    class Backend:
        async def search(self, query, limit):
            assert query == diagnostics.DIAGNOSTIC_QUERY
            return [Paper(id="P", title="Paper", source="arxiv",
                          pdf_url="https://arxiv.org/pdf/test")]

    async def fake_probe(url, timeout=8):
        assert url.startswith("https://arxiv.org/")
        return True, "ok"

    monkeypatch.setitem(diagnostics.BACKENDS, "arxiv", Backend())
    monkeypatch.setattr(diagnostics, "probe_pdf_url", fake_probe)
    rows = asyncio.run(diagnostics.run_connectivity(["arxiv"]))
    assert rows[0]["source"] == "arxiv"
    assert rows[0]["status"] == "ok"
    assert rows[0]["result_count"] == 1
    assert rows[0]["pdf_probe_status"] == "ok"


def test_unknown_diagnostic_target_is_rejected():
    with pytest.raises(ValueError, match="未知检测目标"):
        asyncio.run(diagnostics.run_connectivity(["http://127.0.0.1/"]))
    with pytest.raises(ValueError, match="未知检测目标"):
        asyncio.run(diagnostics.run_download_speed(["file:///etc/passwd"]))


def test_speed_test_caps_ignored_range_and_checks_pdf(monkeypatch):
    monkeypatch.setattr(diagnostics, "_is_safe_url", lambda _url: (True, ""))

    class Response:
        status_code = 200
        headers = {}
        async def aiter_bytes(self):
            yield b"%PDF-1.7" + b"x" * (diagnostics.DOWNLOAD_MAX_BYTES * 2)

    class Stream:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *_args): return False

    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        def stream(self, *args, **kwargs):
            assert kwargs["headers"]["Range"].startswith("bytes=0-")
            return Stream()

    monkeypatch.setattr(diagnostics.httpx, "AsyncClient", Client)
    result = asyncio.run(diagnostics._speed_test_url(
        "arxiv", "https://arxiv.org/pdf/1706.03762"))
    assert result["status"] == "ok"
    assert result["pdf_magic_valid"] is True
    assert result["bytes_read"] == diagnostics.DOWNLOAD_MAX_BYTES
    assert result["domain"] == "arxiv.org"


def test_speed_test_rejects_unsafe_url(monkeypatch):
    monkeypatch.setattr(diagnostics, "_is_safe_url", lambda _url: (False, "private"))
    result = asyncio.run(diagnostics._speed_test_url("arxiv", "http://127.0.0.1/a.pdf"))
    assert result["status"] == "failed"
    assert result["bytes_read"] == 0
