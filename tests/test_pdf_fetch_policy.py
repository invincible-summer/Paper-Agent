from __future__ import annotations

import asyncio

import pytest

from core.models import Paper
import core.paper_search_settings_store as policy_store
from tools.pdf.fetcher import PDFFetcher


@pytest.fixture()
def isolated_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_store, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(policy_store, "_seed", lambda: policy_store.PaperSearchPolicy(
        sources={name: True for name in policy_store.SOURCE_IDS}))
    policy_store.reset_cache()
    yield tmp_path
    policy_store.reset_cache()


def _update(changes):
    current = policy_store.get_paper_search_policy()
    return policy_store.update_paper_search_policy(
        changes, expected_version=current.version, updated_by="test")


def test_disabled_mode_blocks_remote_candidates_but_keeps_local_pdf(isolated_policy):
    _update({"paper_fetch_mode": "disabled"})
    paper = Paper(id="P", title="Paper", source="arxiv", pdf_url="https://arxiv.org/pdf/x")
    fetcher = PDFFetcher(str(isolated_policy / "pdfs"))
    try:
        assert asyncio.run(fetcher.candidate_urls(paper)) == []
        local = isolated_policy / "pdfs" / "P.pdf"
        local.write_bytes(b"%PDF-1.4 cached")
        paper.pdf_path = str(local)
        assert asyncio.run(fetcher.fetch(paper)) == str(local)
    finally:
        asyncio.run(fetcher.close())


def test_explicit_only_allows_explicit_not_automatic(isolated_policy, monkeypatch):
    _update({"paper_fetch_mode": "explicit_only"})
    paper = Paper(id="P", title="Paper", source="arxiv", pdf_url="https://example.org/p.pdf")

    automatic = PDFFetcher(str(isolated_policy / "auto"), fetch_origin="automatic")
    explicit = PDFFetcher(str(isolated_policy / "explicit"), fetch_origin="explicit")
    calls = []
    async def fake_download(url, local_path, title=""):
        calls.append(url)
        local_path.write_bytes(b"%PDF-1.4")
        return str(local_path)
    monkeypatch.setattr(explicit, "_download", fake_download)
    try:
        assert asyncio.run(automatic.fetch(paper)) is None
        assert asyncio.run(explicit.fetch(paper))
        assert calls == [paper.pdf_url]
    finally:
        asyncio.run(automatic.close()); asyncio.run(explicit.close())


def test_search_switch_does_not_change_independent_fulltext_capability(isolated_policy, monkeypatch):
    _update({"sources": {"openalex": False}})
    paper = Paper(id="P", title="Paper", source="openalex", doi="10.1/x",
                  pdf_url="https://openalex.example/direct.pdf")
    fetcher = PDFFetcher(str(isolated_policy / "pdfs"))
    async def fake_unpaywall(_doi):
        return "https://repository.example/oa.pdf"
    monkeypatch.setattr(fetcher, "_unpaywall_pdf_url", fake_unpaywall)
    try:
        assert asyncio.run(fetcher.candidate_urls(paper)) == ["https://openalex.example/direct.pdf"]
    finally:
        asyncio.run(fetcher.close())


def test_fulltext_capability_blocks_source_url_and_unpaywall_before_network(isolated_policy, monkeypatch):
    current = policy_store.get_paper_search_policy()
    policy_store.set_source_capability("openalex", "fulltext", False,
        expected_version=current.version, updated_by="test")
    paper = Paper(id="P", title="Paper", source="openalex", doi="10.1/x",
                  pdf_url="https://openalex.example/direct.pdf")
    fetcher = PDFFetcher(str(isolated_policy / "pdfs"))
    called = False
    async def fake_unpaywall(_doi):
        nonlocal called; called = True; return "https://repository.example/oa.pdf"
    monkeypatch.setattr(fetcher, "_unpaywall_pdf_url", fake_unpaywall)
    try:
        assert asyncio.run(fetcher.candidate_urls(paper)) == []
        assert called is False
    finally:
        asyncio.run(fetcher.close())


def test_unpaywall_auxiliary_capability_blocks_lookup(tmp_path, monkeypatch):
    import asyncio
    import core.paper_search_settings_store as store
    from tools.pdf.fetcher import PDFFetcher

    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "users.db")
    store.reset_cache()
    policy = store.get_paper_search_policy()
    store.set_source_capability(
        "unpaywall", "fulltext", False,
        expected_version=policy.version, updated_by="test",
    )

    fetcher = PDFFetcher.__new__(PDFFetcher)
    fetcher.pdf_dir = tmp_path
    fetcher.admin_override = False

    class ExplodingClient:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("Unpaywall network request must be skipped")

    fetcher._client = ExplodingClient()
    assert asyncio.run(fetcher._unpaywall_pdf_url("10.1000/example")) is None
