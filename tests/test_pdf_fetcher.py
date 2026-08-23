"""Retired network-PDF boundary plus retained filename/URL safety helpers."""

from __future__ import annotations

import asyncio

from core.models import Paper
from tools.pdf.fetcher import (
    MAX_PDF_BYTES,
    PDFFetcher,
    _is_safe_url,
    _sanitize_filename_component,
    probe_pdf_url,
)


def test_url_safety_helper_still_blocks_local_targets(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (
            "93.184.216.34" if host == "public.example" else "127.0.0.1", 443,
        )),
    ])
    assert _is_safe_url("https://public.example/file.pdf")[0] is True
    assert _is_safe_url("https://localhost/file.pdf")[0] is False
    assert _is_safe_url("file:///etc/passwd")[0] is False


def test_filename_sanitizer_and_upload_size_constant_are_retained():
    assert _sanitize_filename_component("doi:10.1000/../evil") == "doi_10.1000_.._evil"
    assert "/" not in _sanitize_filename_component("doi:10.1000/evil")
    assert _sanitize_filename_component("") == "paper"
    assert len(_sanitize_filename_component("x" * 500)) <= 120
    assert MAX_PDF_BYTES == 50 * 1024 * 1024


def test_probe_is_inert_and_does_not_construct_http_client(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("remote probe must not construct an HTTP client")
    ))
    assert asyncio.run(probe_pdf_url("https://example.org/paper.pdf")) == (
        False, "remote_fulltext_retired",
    )


def test_fetcher_never_returns_remote_or_cached_network_pdf(tmp_path):
    fetcher = PDFFetcher(tmp_path)
    cached = tmp_path / "P.pdf"
    cached.write_bytes(b"%PDF-1.4 legacy")
    paper = Paper(
        id="P", title="Paper", pdf_url="https://example.org/p.pdf",
        pdf_path=str(cached),
    )
    assert asyncio.run(fetcher.candidate_urls(paper)) == []
    assert asyncio.run(fetcher.fetch(paper)) is None
    assert asyncio.run(fetcher.fetch_many([paper])) == {}
    asyncio.run(fetcher.close())
