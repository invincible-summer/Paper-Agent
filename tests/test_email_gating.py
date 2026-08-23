"""Contact identity, API-key and OA lookup gating tests."""
from __future__ import annotations
import asyncio
import types


def _fake_settings(openalex_api_key="", contact="", openalex_email="", crossref_email=""):
    return types.SimpleNamespace(search=types.SimpleNamespace(
        openalex_api_key=openalex_api_key, paper_platform_contact_email=contact,
        openalex_email=openalex_email, crossref_email=crossref_email))

class _FakeResp:
    status_code=200; history=[]
    headers={"content-type":"application/json"}
    def json(self): return {"results":[],"message":{"items":[]}}

class _FakeClient:
    captured={}
    async def request(self,method,url,params=None,**kw):
        self.captured={"params":params or {},"headers":kw.get("headers") or {}}
        _FakeClient.captured=self.captured
        return _FakeResp()


def test_openalex_disabled_without_api_key(monkeypatch):
    import tools.search.openalex as mod
    client=_FakeClient(); monkeypatch.setattr(mod,"get_settings",lambda:_fake_settings())
    monkeypatch.setattr(mod,"get_search_http_client",lambda:client)
    assert asyncio.run(mod.OpenAlexBackend().search("graph",5))==[]
    assert _FakeClient.captured=={}


def test_openalex_sends_api_key_not_mailto(monkeypatch):
    import tools.search.openalex as mod
    client=_FakeClient(); monkeypatch.setattr(mod,"get_settings",lambda:_fake_settings(openalex_api_key="key",openalex_email="old@x"))
    monkeypatch.setattr(mod,"get_search_http_client",lambda:client)
    asyncio.run(mod.OpenAlexBackend().search("graph",5))
    assert _FakeClient.captured["params"]["api_key"]=="key"
    assert "mailto" not in _FakeClient.captured["params"]


def test_crossref_uses_unified_contact(monkeypatch):
    import tools.search.crossref as mod
    client=_FakeClient(); monkeypatch.setattr(mod,"get_settings",lambda:_fake_settings(contact="me@uni.edu"))
    monkeypatch.setattr(mod,"get_search_http_client",lambda:client)
    asyncio.run(mod.CrossrefBackend().search("graph",5))
    assert _FakeClient.captured["params"]["mailto"]=="me@uni.edu"
    assert "me@uni.edu" in _FakeClient.captured["headers"]["User-Agent"]


def test_openalex_refs_auth_helper(monkeypatch):
    import tools.search.openalex_refs as mod
    monkeypatch.setattr(mod,"get_settings",lambda:_fake_settings())
    assert mod._auth_params()=={}
    monkeypatch.setattr(mod,"get_settings",lambda:_fake_settings(openalex_api_key="key"))
    assert mod._auth_params()=={"api_key":"key"}


def test_network_pdf_fetcher_is_inert_without_any_unpaywall_path(monkeypatch,tmp_path):
    from tools.pdf.fetcher import PDFFetcher
    f=PDFFetcher.__new__(PDFFetcher);f.pdf_dir=tmp_path
    class Exploding:
        async def get(self,*a,**k):raise AssertionError("must not call")
    f._client=Exploding();monkeypatch.setattr("core.config.get_settings",lambda:_fake_settings())
    assert not hasattr(f, "_unpaywall_pdf_url")
    assert asyncio.run(f.candidate_urls(types.SimpleNamespace(id="p1"))) == []
