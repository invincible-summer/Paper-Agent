"""Remote full-text settings and fetch modes are no longer runtime policy."""

from __future__ import annotations

import asyncio

import pytest

import core.paper_search_settings_store as policy_store
from core.models import Paper
from tools.pdf.fetcher import PDFFetcher


def test_policy_exposes_only_search_and_abstract_capabilities(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_store, "_DB_PATH", tmp_path / "users.db")
    policy_store.reset_cache()
    policy = policy_store.get_paper_search_policy()
    assert policy_store.PAPER_CAPABILITIES == ("search", "abstract")
    assert not hasattr(policy, "paper_fetch_mode")
    with pytest.raises(policy_store.PaperSearchSettingsError):
        policy_store.set_source_capability(
            "arxiv", "fulltext", False,
            expected_version=policy.version, updated_by="test",
        )


def test_fetcher_is_inert_regardless_of_legacy_paper_fields(tmp_path):
    paper = Paper(
        id="P", title="Paper", source="arxiv",
        pdf_url="https://arxiv.org/pdf/1706.03762.pdf",
        pdf_path=str(tmp_path / "old.pdf"),
    )
    (tmp_path / "old.pdf").write_bytes(b"%PDF-1.4 legacy")
    fetcher = PDFFetcher(tmp_path)
    assert asyncio.run(fetcher.candidate_urls(paper)) == []
    assert asyncio.run(fetcher.fetch(paper)) is None
