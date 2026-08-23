"""Network-paper full-text availability is retired."""
from __future__ import annotations

import asyncio
from pathlib import Path

from core.models import Paper
from tools.pdf.availability import verify_papers_fulltext
from tools.pdf.fetcher import PDFFetcher, probe_pdf_url


def test_availability_shim_never_probes_or_returns_statuses():
    paper = Paper(id="p", source="openalex", abstract="摘要")
    assert asyncio.run(verify_papers_fulltext([paper])) == {}
    assert asyncio.run(probe_pdf_url("https://example.org/p.pdf")) == (False, "remote_fulltext_retired")


def test_fetcher_stub_never_returns_network_pdf(tmp_path: Path):
    fetcher = PDFFetcher(tmp_path)
    paper = Paper(id="p", source="openalex")
    assert asyncio.run(fetcher.candidate_urls(paper)) == []
    assert asyncio.run(fetcher.fetch(paper)) is None
    assert asyncio.run(fetcher.fetch_many([paper])) == {}
    asyncio.run(fetcher.close())
