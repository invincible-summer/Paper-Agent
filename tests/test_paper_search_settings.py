from __future__ import annotations

import sqlite3

import pytest

import core.paper_search_settings_store as store


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(store, "_seed", lambda: store.PaperSearchPolicy(
        sources={name: name != "core" for name in store.SOURCE_IDS}))
    store.reset_cache()
    yield
    store.reset_cache()


def test_initial_seed_and_idempotent_read(isolated):
    policy = store.get_paper_search_policy()
    assert policy.sources["core"] is False
    assert policy.search_deadline_seconds == 30
    again = store.get_paper_search_policy()
    assert again == policy


def test_update_is_immediate_and_optimistically_locked(isolated):
    current = store.get_paper_search_policy()
    updated = store.update_paper_search_policy(
        {"sources": {"openalex": False}, "paper_fetch_mode": "probe_only",
         "fetch_policy_disclosure": "silent", "search_deadline_seconds": 45},
        expected_version=current.version, updated_by="admin",
    )
    assert updated.sources["openalex"] is False
    assert updated.sources["arxiv"] is True
    assert updated.paper_fetch_mode == "probe_only"
    assert updated.fetch_policy_disclosure == "silent"
    assert updated.version == current.version + 1
    with pytest.raises(store.PaperSearchVersionConflict):
        store.update_paper_search_policy(
            {"verify_fulltext": False}, expected_version=current.version,
            updated_by="stale")


def test_validation_rejects_unknown_source_and_bad_budget(isolated):
    current = store.get_paper_search_policy()
    with pytest.raises(store.PaperSearchSettingsError, match="未知论文渠道"):
        store.update_paper_search_policy(
            {"sources": {"made_up": False}}, expected_version=current.version,
            updated_by="admin")
    with pytest.raises(store.PaperSearchSettingsError, match="不能大于"):
        store.update_paper_search_policy(
            {"per_source_timeout_seconds": 30, "search_deadline_seconds": 20},
            expected_version=current.version, updated_by="admin")


def test_diagnostics_keep_latest_twenty_without_secrets(isolated):
    for i in range(22):
        store.save_diagnostic_run(
            "connectivity", i, i + .5, {"count": 1},
            [{"source": "arxiv", "status": "ok", "http_status": 200}],
        )
    data = store.get_latest_diagnostics()
    assert len(data["recent"]) == 20
    assert data["connectivity"]["items"][0]["source"] == "arxiv"
    conn = sqlite3.connect(store._DB_PATH)
    try:
        payload = " ".join(row[0] for row in conn.execute(
            "SELECT payload_json FROM paper_search_diagnostic_items"))
    finally:
        conn.close()
    assert "Authorization" not in payload
