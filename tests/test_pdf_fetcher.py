"""Tests for the PDF fetcher SSRF guard + filename sanitization (D-089)."""

import asyncio
import types
from contextlib import asynccontextmanager
from pathlib import Path

from core.models import Paper
from tools.pdf.fetcher import (
    MAX_PDF_BYTES,
    PDFFetcher,
    _is_safe_url,
    _sanitize_filename_component,
    probe_pdf_url,
)


def test_safe_public_https_url(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.3.42", 443))
    ])
    ok, reason = _is_safe_url("https://arxiv.org/pdf/2301.00001")
    assert ok, reason


def test_blocks_non_http_scheme():
    ok, reason = _is_safe_url("file:///etc/passwd")
    assert not ok
    assert "scheme" in reason
    assert _is_safe_url("gopher://x")[0] is False


def test_blocks_loopback_and_private(monkeypatch):
    import socket

    def fake_getaddrinfo(host, *a, **k):
        if host == "public.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        if host == "inside.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
        if host == "meta.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
        raise socket.gaierror

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert _is_safe_url("http://public.example.com/x.pdf")[0] is True
    assert _is_safe_url("http://inside.example.com/x.pdf")[0] is False  # private
    assert _is_safe_url("http://meta.example.com/x.pdf")[0] is False    # metadata
    assert _is_safe_url("http://localhost/x.pdf")[0] is False
    assert _is_safe_url("http://127.0.0.1/x.pdf")[0] is False


def test_unresolvable_host_rejected(monkeypatch):
    import socket

    def boom(host, *a, **k):
        raise socket.gaierror

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert _is_safe_url("https://does-not-exist.invalid/x")[0] is False


def test_sanitize_filename_strips_traversal():
    assert "/" not in _sanitize_filename_component("doi:10.1000/evil")
    assert ".." not in _sanitize_filename_component("../sneaky")
    assert _sanitize_filename_component("") == "paper"
    assert _sanitize_filename_component("doi:10.1000/abc") == "doi_10.1000_abc"
    assert len(_sanitize_filename_component("x" * 500)) <= 120


def test_max_pdf_bytes_cap_is_sane():
    assert MAX_PDF_BYTES == 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# probe_pdf_url — lightweight availability check (Range + PDF magic, no download)
# ---------------------------------------------------------------------------

class _FakeStreamResponse:
    def __init__(self, status=200, body=b"%PDF-1.7 fake body",
                 headers=None, redirect=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.redirect = redirect

    async def aiter_bytes(self):
        yield self.body

    async def aclose(self):
        pass


class _FakeProbeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("probe made more requests than expected")
        resp = self.responses.pop(0)
        try:
            yield resp
        finally:
            await resp.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def _probe_ok(monkeypatch, responses):
    client = _FakeProbeClient(responses)
    import httpx as httpx_mod
    monkeypatch.setattr(httpx_mod, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr("tools.pdf.fetcher._is_safe_url", lambda url: (True, ""))
    return client


def test_probe_accepts_pdf_magic_without_full_download(monkeypatch):
    client = _probe_ok(monkeypatch, [_FakeStreamResponse(body=b"%PDF-1.7 first bytes")])
    ok, reason = asyncio.run(probe_pdf_url("https://oa.example/p.pdf"))
    assert (ok, reason) == (True, "ok")
    assert client.calls[0][2].get("headers", {}).get("Range") == "bytes=0-8191"


def test_probe_rejects_html_landing_page(monkeypatch):
    client = _probe_ok(monkeypatch, [_FakeStreamResponse(
        status=200, body=b"<!doctype html><title>login</title>",
        headers={"content-type": "text/html"})])
    assert asyncio.run(probe_pdf_url("https://publisher.example/pdf/landing")) \
        == (False, "not_pdf")


def test_probe_follows_redirect_to_pdf(monkeypatch):
    client = _probe_ok(monkeypatch, [
        _FakeStreamResponse(status=302, headers={"location": "/real.pdf"}),
        _FakeStreamResponse(body=b"%PDF-1.4 real"),
    ])
    assert asyncio.run(probe_pdf_url("https://doi.org/10.1/x")) == (True, "ok")
    assert client.calls[1][1] == "https://doi.org/real.pdf"


def test_probe_http_404_is_a_definite_negative(monkeypatch):
    _probe_ok(monkeypatch, [_FakeStreamResponse(status=404)])
    assert asyncio.run(probe_pdf_url("https://publisher.example/missing")) \
        == (False, "http_404")


def test_probe_retries_without_range_when_server_rejects_it(monkeypatch):
    client = _probe_ok(monkeypatch, [
        _FakeStreamResponse(status=416),
        _FakeStreamResponse(body=b"%PDF-1.4 plain get"),
    ])
    assert asyncio.run(probe_pdf_url("https://oa.example/quirky.pdf")) == (True, "ok")
    assert client.calls[0][2].get("headers") == {"Range": "bytes=0-8191"}
    assert client.calls[1][2].get("headers") is None


# ---------------------------------------------------------------------------
# OA candidate ordering + Unpaywall (offline; HTTP stubbed)
# ---------------------------------------------------------------------------

def _fetcher(tmp_path, downloads=None, unpaywall=None):
    """PDFFetcher with stubbed _download / _unpaywall_pdf_url.

    downloads: {url: bool} — whether each url "downloads" successfully.
    unpaywall: return value of the DOI lookup.
    """
    f = PDFFetcher.__new__(PDFFetcher)
    f.pdf_dir = tmp_path
    f._client = None
    f.tried = []

    async def _download(url, local_path, title=""):
        f.tried.append(url)
        return str(local_path) if (downloads or {}).get(url) else None

    async def _unpaywall(doi):
        f.unpaywall_doi = doi
        return unpaywall

    f._download = _download
    f._unpaywall_pdf_url = _unpaywall
    return f


def test_fetch_tries_source_url_first_then_unpaywall(tmp_path):
    f = _fetcher(tmp_path,
                 downloads={"https://arxiv.org/pdf/1": None,
                            "https://oa.example.org/p.pdf": True},
                 unpaywall="https://oa.example.org/p.pdf")
    paper = Paper(id="P1", title="t", pdf_url="https://arxiv.org/pdf/1",
                  doi="10.1000/x")
    out = asyncio.run(f.fetch(paper))
    assert out is not None
    assert f.tried == ["https://arxiv.org/pdf/1", "https://oa.example.org/p.pdf"]


def test_fetch_unpaywall_rescues_missing_pdf_url(tmp_path):
    f = _fetcher(tmp_path,
                 downloads={"https://oa.example.org/p.pdf": True},
                 unpaywall="https://oa.example.org/p.pdf")
    paper = Paper(id="P1", title="t", pdf_url=None, doi="10.1000/x")
    assert asyncio.run(f.fetch(paper)) is not None
    assert f.unpaywall_doi == "10.1000/x"


def test_fetch_no_candidates_returns_none(tmp_path):
    f = _fetcher(tmp_path, unpaywall=None)
    paper = Paper(id="P1", title="t", pdf_url=None, doi="10.1000/x")
    assert asyncio.run(f.fetch(paper)) is None
    assert f.tried == []


def test_downloaded_pdf_persists_and_is_reused_across_calls(tmp_path):
    """deep_read downloads must be kept on disk and reused, never deleted."""
    f = PDFFetcher.__new__(PDFFetcher)
    f.pdf_dir = tmp_path
    f._artifact_store = None
    f.tried = []

    async def _unpaywall(doi):
        return None

    async def _download(url, local_path, title=""):
        f.tried.append(url)
        local_path.write_bytes(b"%PDF-1.4 deep-read download")
        return str(local_path)

    f._unpaywall_pdf_url = _unpaywall
    f._download = _download
    paper = Paper(id="P1", title="t", pdf_url="https://oa.example/p.pdf")

    first = asyncio.run(f.fetch(paper))
    second = asyncio.run(f.fetch(paper))
    assert first == second
    assert Path(first).is_file() and Path(first).read_bytes()[:5] == b"%PDF-"
    # The second fetch is a local-cache hit: no second network download.
    assert f.tried == ["https://oa.example/p.pdf"]


def test_api_channel_redownloads_after_public_pdf_expires(tmp_path, monkeypatch):
    """API public_pdf expires after TTL; the next deep_read must automatically
    re-download and re-register the same paper (no stale expired reuse)."""
    from core.storage_context import StorageContext
    from core.api_artifact_store import ApiArtifactStore

    context = StorageContext.openai_api(root_dir=tmp_path / "openai-api")
    paper = Paper(id="doi:10.1000/expired", title="Expired PDF",
                  pdf_url="https://oa.example/expired.pdf")
    fetcher = PDFFetcher("unused", storage_context=context)
    logical = "doi_10.1000_expired.pdf"

    # Simulate a previously deep-read copy whose 3-day TTL has expired.
    source = context.temp_dir / "old.pdf"
    source.write_bytes(b"%PDF-1.4 old")
    old = fetcher._artifact_store.save_public_pdf(source, logical_name=logical)
    with fetcher._artifact_store.storage.connect() as conn:
        conn.execute("UPDATE api_artifacts SET expires_at=0 WHERE id=?", (old.id,))
        conn.commit()
    assert fetcher._artifact_store.find_active(
        category="public_pdf", scope="public", logical_name=logical) is None

    async def fake_download_to_temp(url, *, temp_dir=None, max_bytes=None):
        path = Path(temp_dir) / f"redownload-{old.id}.tmp"
        path.write_bytes(b"%PDF-1.4 fresh")
        return path, "application/pdf"

    monkeypatch.setattr("tools.ingest.downloader.download_to_temp", fake_download_to_temp)

    try:
        path = asyncio.run(fetcher.fetch(paper))
    finally:
        asyncio.run(fetcher.close())

    assert path is not None and Path(path).is_file()
    assert Path(path).read_bytes()[:5] == b"%PDF-"
    # Same SHA-256 blob was re-registered with a fresh expiry.
    again = fetcher._artifact_store.find_active(
        category="public_pdf", scope="public", logical_name=logical)
    assert again is not None
    with fetcher._artifact_store.storage.connect() as conn:
        expires_at = conn.execute(
            "SELECT expires_at FROM api_artifacts WHERE id=?", (again.id,)
        ).fetchone()[0]
    assert expires_at > 0


def test_unpaywall_url_parsing_and_email_gate(monkeypatch, tmp_path):
    f = PDFFetcher.__new__(PDFFetcher)
    f.pdf_dir = tmp_path

    class _Resp:
        status_code = 200

        def json(self):
            return {"is_oa": True,
                    "best_oa_location": {"url_for_pdf": "https://oa.example.org/x.pdf"}}

    class _Client:
        def __init__(self):
            self.calls = []

        async def get(self, url, params=None):
            self.calls.append((url, params))
            return _Resp()

    f._client = _Client()
    import core.config as cfg
    fake = types.SimpleNamespace(search=types.SimpleNamespace(
        openalex_email="a@b.edu", crossref_email=""))
    monkeypatch.setattr(cfg, "get_settings", lambda: fake)

    url = asyncio.run(f._unpaywall_pdf_url("https://doi.org/10.1000/x"))
    assert url == "https://oa.example.org/x.pdf"
    assert "10.1000/x" in f._client.calls[0][0]
    assert f._client.calls[0][1] == {"email": "a@b.edu"}

    # No email configured -> Unpaywall is never called.
    fake_no = types.SimpleNamespace(search=types.SimpleNamespace(
        openalex_email="", crossref_email=""))
    monkeypatch.setattr(cfg, "get_settings", lambda: fake_no)
    f._client.calls.clear()
    assert asyncio.run(f._unpaywall_pdf_url("10.1000/x")) is None
    assert f._client.calls == []

    # Not OA -> None.
    class _ClosedResp:
        status_code = 200

        def json(self):
            return {"is_oa": False}

    class _ClosedClient:
        async def get(self, url, params=None):
            return _ClosedResp()

    monkeypatch.setattr(cfg, "get_settings", lambda: fake)
    f._client = _ClosedClient()
    assert asyncio.run(f._unpaywall_pdf_url("10.1000/x")) is None


def test_europepmc_oa_only():
    """Subscription links are never returned as pdf candidates."""
    from tools.search.europepmc import _extract_pdf_url

    oa_item = {"fullTextUrlList": {"fullTextUrl": [
        {"documentStyle": "pdf", "url": "https://pub.example.com/paywalled.pdf",
         "availability": "Subscription required"},
        {"documentStyle": "pdf", "url": "https://pmc.example.org/oa.pdf",
         "availability": "Open access", "availabilityCode": "OA"},
    ]}}
    assert _extract_pdf_url(oa_item) == "https://pmc.example.org/oa.pdf"

    closed_item = {"fullTextUrlList": {"fullTextUrl": [
        {"documentStyle": "pdf", "url": "https://pub.example.com/paywalled.pdf",
         "availability": "Subscription required"},
    ]}}
    assert _extract_pdf_url(closed_item) is None
