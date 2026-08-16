"""Email-parameter gating tests (compliance).

Rule: when no real email is configured, OpenAlex/Crossref are accessed
anonymously (no mailto param at all — never a placeholder identity);
Unpaywall is not called at all. When a real email is configured it is sent.
"""
from __future__ import annotations

import asyncio
import types

import httpx


def _fake_settings(openalex_email="", crossref_email=""):
    return types.SimpleNamespace(search=types.SimpleNamespace(
        openalex_email=openalex_email, crossref_email=crossref_email))


class _FakeResp:
    status_code = 200

    def json(self):
        return {"results": []}


class _FakeClient:
    captured = {}

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def request(self, method, url, params=None, **kw):
        _FakeClient.captured["params"] = params or {}
        return _FakeResp()


def test_openalex_anonymous_without_email(monkeypatch):
    import tools.search.openalex as mod

    monkeypatch.setattr(mod, "get_settings", lambda: _fake_settings())
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    asyncio.run(mod.OpenAlexBackend().search("graph", 5))
    assert "mailto" not in _FakeClient.captured["params"]


def test_openalex_mailto_with_real_email(monkeypatch):
    import tools.search.openalex as mod

    monkeypatch.setattr(mod, "get_settings",
                        lambda: _fake_settings(openalex_email="me@uni.edu"))
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    asyncio.run(mod.OpenAlexBackend().search("graph", 5))
    assert _FakeClient.captured["params"].get("mailto") == "me@uni.edu"


def test_crossref_anonymous_without_email(monkeypatch):
    import tools.search.crossref as mod

    monkeypatch.setattr(mod, "get_settings", lambda: _fake_settings())
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    asyncio.run(mod.CrossrefBackend().search("graph", 5))
    assert "mailto" not in _FakeClient.captured["params"]


def test_openalex_refs_mailto_helper(monkeypatch):
    import tools.search.openalex_refs as mod

    monkeypatch.setattr(mod, "get_settings", lambda: _fake_settings())
    assert mod._mailto() == {}
    monkeypatch.setattr(mod, "get_settings",
                        lambda: _fake_settings(openalex_email="me@uni.edu"))
    assert mod._mailto() == {"mailto": "me@uni.edu"}


def test_unpaywall_not_called_without_email(monkeypatch, tmp_path):
    """Unpaywall REQUIRES an email param: without one, zero requests."""
    from tools.pdf.fetcher import PDFFetcher

    f = PDFFetcher.__new__(PDFFetcher)
    f.pdf_dir = tmp_path

    class _ExplodingClient:
        async def get(self, *a, **k):
            raise AssertionError("Unpaywall must not be called without email")

    f._client = _ExplodingClient()
    monkeypatch.setattr("core.config.get_settings", lambda: _fake_settings())
    assert asyncio.run(f._unpaywall_pdf_url("10.1000/x")) is None
