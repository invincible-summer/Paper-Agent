"""The former arXiv downloader remains import-compatible but inert."""

from __future__ import annotations

import asyncio

from tools.pdf.arxiv_wget import ArxivFetchResult, fetch_arxiv_pdf


def test_arxiv_adapter_never_starts_a_process(monkeypatch, tmp_path):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *_a, **_k: (
        _ for _ in ()
    ).throw(AssertionError("retired adapter must not start wget")))
    destination = tmp_path / "paper.pdf"
    result = asyncio.run(fetch_arxiv_pdf(
        "https://arxiv.org/pdf/1706.03762.pdf", destination=destination,
    ))
    assert result == ArxivFetchResult()
    assert result.error_code == "remote_fulltext_retired"
    assert not destination.exists()
