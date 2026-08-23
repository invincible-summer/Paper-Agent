"""Persistent search/abstract capability policy (no remote full-text knobs)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import core.paper_search_settings_store as store


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "users.db")
    store.reset_cache()
    yield
    store.reset_cache()


def test_policy_exposes_only_search_and_abstract(isolated):
    policy = store.get_paper_search_policy()
    assert set(store.PAPER_CAPABILITIES) == {"search", "abstract"}
    assert all(set(row) == {"search", "abstract"} for row in policy.capabilities.values())
    public = store.policy_dict(policy)
    assert "fulltext" not in str(public).lower()
    assert "paper_fetch_mode" not in public


def test_toggle_search_and_abstract_is_optimistic_and_immediate(isolated):
    current = store.get_paper_search_policy()
    updated = store.set_source_capability("arxiv", "abstract", False,
                                           expected_version=current.version,
                                           updated_by="admin")
    assert updated.capabilities["arxiv"]["abstract"]["enabled"] is False
    assert store.paper_abstract_text(type("P", (), {"source": "arxiv", "abstract": "摘要"})()) == ""
    with pytest.raises(store.PaperSearchVersionConflict):
        store.set_source_capability("arxiv", "search", False,
                                    expected_version=current.version,
                                    updated_by="stale")


def test_diagnostic_updates_only_supported_capabilities(isolated):
    run = store.apply_diagnostic_results([
        {"source": "arxiv", "capability": "abstract", "status": "failed",
         "latency_ms": 123, "error_code": "abstract_empty", "auto_disable": True}
    ], started_at=1, finished_at=2)
    assert run.kind == "capability"
    policy = store.get_paper_search_policy()
    assert policy.capabilities["arxiv"]["abstract"]["enabled"] is False
    assert "fulltext" not in policy.capabilities["arxiv"]


def test_unknown_or_fulltext_capability_is_rejected(isolated):
    policy = store.get_paper_search_policy()
    with pytest.raises(store.PaperSearchSettingsError):
        store.set_source_capability("arxiv", "fulltext", False,
                                    expected_version=policy.version,
                                    updated_by="admin")
    with pytest.raises(store.PaperSearchSettingsError):
        store.source_capability_status("arxiv", "citations")


def test_legacy_columns_are_migrated_away_without_losing_current_state(tmp_path: Path, monkeypatch):
    path = tmp_path / "users.db"
    capabilities = store.capability_defaults({"arxiv": True})
    capabilities["arxiv"]["abstract"]["enabled"] = False
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE paper_search_policy (
            id INTEGER PRIMARY KEY, sources_json TEXT NOT NULL,
            capabilities_json TEXT NOT NULL, search_deadline_seconds INTEGER NOT NULL,
            per_source_timeout_seconds INTEGER NOT NULL, verify_fulltext INTEGER NOT NULL,
            fulltext_verify_timeout_seconds INTEGER NOT NULL,
            force_fulltext_probe INTEGER NOT NULL, paper_fetch_mode TEXT NOT NULL,
            fetch_policy_disclosure TEXT NOT NULL, routing_mode TEXT NOT NULL,
            version INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at REAL NOT NULL
        )""")
        conn.execute(
            "INSERT INTO paper_search_policy VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (json.dumps({"arxiv": True}), json.dumps(capabilities), 28, 9,
             1, 12, 1, "automatic", "verbose", "smart", 7, "legacy", 123.0),
        )
    monkeypatch.setattr(store, "_DB_PATH", path)
    store.reset_cache()
    policy = store.get_paper_search_policy()
    with sqlite3.connect(path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_search_policy)")}
    assert cols == {
        "id", "sources_json", "capabilities_json", "search_deadline_seconds",
        "per_source_timeout_seconds", "routing_mode", "version", "updated_by",
        "updated_at",
    }
    assert policy.search_deadline_seconds == 28
    assert policy.per_source_timeout_seconds == 9
    assert policy.version == 7
    assert policy.capabilities["arxiv"]["abstract"]["enabled"] is False
    assert "fulltext" not in str(store.policy_dict(policy)).lower()
