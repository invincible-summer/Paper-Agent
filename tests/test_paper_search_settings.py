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
    assert policy.capabilities["core"]["search"]["enabled"] is False
    assert policy.capabilities["arxiv"]["abstract"]["enabled"] is True
    assert policy.capabilities["dblp"]["abstract"]["enabled"] is False
    assert store.get_paper_search_policy() == policy


def test_three_capabilities_are_independent_and_manual_recovery_keeps_history(isolated):
    current = store.get_paper_search_policy()
    closed = store.set_source_capability(
        "arxiv", "abstract", False, expected_version=current.version, updated_by="admin")
    assert closed.sources["arxiv"] is True
    assert closed.capabilities["arxiv"]["search"]["enabled"] is True
    assert closed.capabilities["arxiv"]["abstract"]["disabled_by"] == "administrator"

    checked = store.apply_diagnostic_results([
        {"source": "arxiv", "capability": "abstract", "status": "failed",
         "latency_ms": 123, "error_code": "abstract_empty", "auto_disable": True},
    ], started_at=1, finished_at=2)
    assert checked.summary["auto_disabled_capabilities"] == []  # already manually closed
    policy = store.get_paper_search_policy()
    assert policy.capabilities["arxiv"]["abstract"]["last_latency_ms"] == 123

    recovered = store.set_source_capability(
        "arxiv", "abstract", True, expected_version=policy.version, updated_by="admin")
    state = recovered.capabilities["arxiv"]["abstract"]
    assert state["enabled"] is True
    assert state["disabled_by"] is None
    assert state["last_diagnostic_status"] == "failed"
    assert state["last_latency_ms"] == 123


def test_diagnostic_failure_closes_only_target_and_success_never_recovers(isolated):
    store.apply_diagnostic_results([
        {"source": "arxiv", "capability": "fulltext", "status": "failed",
         "latency_ms": 800, "kb_per_second": 0, "error_code": "timeout",
         "message": "PDF 下载超时", "auto_disable": True},
    ], started_at=1, finished_at=2)
    policy = store.get_paper_search_policy()
    assert policy.capabilities["arxiv"]["fulltext"]["enabled"] is False
    assert policy.capabilities["arxiv"]["fulltext"]["disabled_by"] == "diagnostic"
    assert policy.capabilities["arxiv"]["search"]["enabled"] is True
    assert policy.capabilities["arxiv"]["abstract"]["enabled"] is True

    store.apply_diagnostic_results([
        {"source": "arxiv", "capability": "fulltext", "status": "ok",
         "latency_ms": 100, "kb_per_second": 500, "auto_disable": False},
    ], started_at=3, finished_at=4)
    state = store.get_paper_search_policy().capabilities["arxiv"]["fulltext"]
    assert state["enabled"] is False
    assert state["last_diagnostic_status"] == "ok"
    assert state["last_kb_per_second"] == 500


def test_update_is_immediate_and_optimistically_locked(isolated):
    current = store.get_paper_search_policy()
    updated = store.update_paper_search_policy(
        {"sources": {"openalex": False}, "paper_fetch_mode": "probe_only",
         "fetch_policy_disclosure": "silent", "search_deadline_seconds": 25},
        expected_version=current.version, updated_by="admin",
    )
    assert updated.sources["openalex"] is False
    assert updated.capabilities["openalex"]["search"]["enabled"] is False
    # Legacy sources switch maps only to search; fulltext remains independent.
    assert updated.capabilities["openalex"]["fulltext"]["enabled"] is True
    assert updated.paper_fetch_mode == "probe_only"
    with pytest.raises(store.PaperSearchVersionConflict):
        store.update_paper_search_policy(
            {"verify_fulltext": False}, expected_version=current.version, updated_by="stale")


def test_validation_rejects_unknown_source_capability_and_bad_budget(isolated):
    current = store.get_paper_search_policy()
    with pytest.raises(store.PaperSearchSettingsError, match="未知论文渠道"):
        store.update_paper_search_policy(
            {"sources": {"made_up": False}}, expected_version=current.version, updated_by="admin")
    with pytest.raises(store.PaperSearchSettingsError, match="不能大于"):
        store.update_paper_search_policy(
            {"per_source_timeout_seconds": 30, "search_deadline_seconds": 20},
            expected_version=current.version, updated_by="admin")
    with pytest.raises(store.PaperSearchSettingsError, match="未知平台能力"):
        store.source_capability_status("arxiv", "citations")


def test_diagnostics_keep_latest_twenty_without_secrets_or_payloads(isolated):
    for i in range(22):
        store.save_diagnostic_run(
            "capability", i, i + .5, {"count": 1},
            [{"source": "arxiv", "status": "ok", "http_status": 200,
              "abstract_length": 120, "bytes_read": 8192}],
        )
    data = store.get_latest_diagnostics()
    assert len(data["recent"]) == 20
    assert "connectivity" not in data and "download" not in data
    conn = sqlite3.connect(store._DB_PATH)
    try:
        payload = " ".join(row[0] for row in conn.execute(
            "SELECT payload_json FROM paper_search_diagnostic_items"))
    finally:
        conn.close()
    assert "Authorization" not in payload
    assert "%PDF-" not in payload
    assert "full abstract body" not in payload


def test_upgrade_maps_old_sources_to_search_only_and_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "users.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE paper_search_policy (
        id INTEGER PRIMARY KEY, sources_json TEXT NOT NULL, search_deadline_seconds INTEGER NOT NULL,
        per_source_timeout_seconds INTEGER NOT NULL, verify_fulltext INTEGER NOT NULL,
        fulltext_verify_timeout_seconds INTEGER NOT NULL, paper_fetch_mode TEXT NOT NULL,
        fetch_policy_disclosure TEXT NOT NULL, version INTEGER NOT NULL, updated_by TEXT NOT NULL,
        updated_at REAL NOT NULL)""")
    conn.execute("INSERT INTO paper_search_policy VALUES(1,?,30,12,1,30,'enabled','affected_only',1,'old',0)",
                 ('{"arxiv":false,"crossref":true}',))
    conn.commit(); conn.close()
    monkeypatch.setattr(store, "_DB_PATH", db); store.reset_cache()
    first = store.get_paper_search_policy()
    second = store.get_paper_search_policy()
    assert first == second
    assert first.capabilities["arxiv"]["search"]["enabled"] is False
    assert first.capabilities["arxiv"]["abstract"]["enabled"] is True
    for source in ("biorxiv", "medrxiv", "pubmed", "datacite", "dblp"):
        assert first.sources[source] is False
    assert first.force_fulltext_probe is True


def test_upgrade_removes_legacy_split_diagnostic_runs(tmp_path, monkeypatch):
    db = tmp_path / "users.db"
    monkeypatch.setattr(store, "_DB_PATH", db)
    store.reset_cache()
    store.get_paper_search_policy()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO paper_search_diagnostic_runs VALUES(?,?,?,?,?)",
        ("old-connectivity", "connectivity", 1, 2, '{"count":0}'),
    )
    conn.execute(
        "INSERT INTO paper_search_diagnostic_runs VALUES(?,?,?,?,?)",
        ("current", "capability", 3, 4, '{"count":0}'),
    )
    conn.commit()
    conn.close()

    store.reset_cache()
    store.get_paper_search_policy()
    conn = sqlite3.connect(db)
    try:
        kinds = [row[0] for row in conn.execute(
            "SELECT kind FROM paper_search_diagnostic_runs ORDER BY id"
        )]
    finally:
        conn.close()
    assert kinds == ["capability"]


def test_auxiliary_unpaywall_fulltext_capability_is_persistent_and_doi_is_not_applicable(isolated):
    policy = store.get_paper_search_policy()
    assert policy.capabilities["unpaywall"]["fulltext"]["enabled"] is True
    assert policy.capabilities["unpaywall"]["search"]["enabled"] is False
    assert policy.capabilities["doi"]["fulltext"]["enabled"] is False
    assert store.source_capability_supported("unpaywall", "fulltext") is True
    assert store.source_capability_supported("doi", "fulltext") is False

    updated = store.set_source_capability(
        "unpaywall", "fulltext", False,
        expected_version=policy.version, updated_by="administrator",
    )
    assert updated.capabilities["unpaywall"]["fulltext"]["enabled"] is False
    assert updated.capabilities["unpaywall"]["fulltext"]["disabled_by"] == "administrator"


def test_single_capability_diagnostic_does_not_mutate_unrelated_history(isolated):
    store.apply_diagnostic_results([
        {"source": "arxiv", "capability": "abstract", "status": "ok",
         "latency_ms": 111, "auto_disable": False},
        {"source": "arxiv", "capability": "fulltext", "status": "ok",
         "latency_ms": 222, "kb_per_second": 333, "auto_disable": False},
    ], started_at=1, finished_at=2)
    store.apply_diagnostic_results([
        {"source": "arxiv", "capability": "search", "status": "failed",
         "latency_ms": 444, "error_code": "search_empty", "auto_disable": True},
    ], started_at=3, finished_at=4)
    caps = store.get_paper_search_policy().capabilities["arxiv"]
    assert caps["search"]["last_latency_ms"] == 444
    assert caps["abstract"]["last_latency_ms"] == 111
    assert caps["fulltext"]["last_latency_ms"] == 222
    assert caps["fulltext"]["last_kb_per_second"] == 333


def test_latest_capability_diagnostic_restores_platform_matrix(isolated):
    platform = {
        "source": "arxiv",
        "connectivity": {"status": "ok", "latency_ms": 10},
        "search": {"status": "ok", "latency_ms": 11, "result_count": 1},
        "abstract": {"status": "ok", "latency_ms": 12, "abstract_length": 100},
        "fulltext": {"status": "ok", "latency_ms": 13, "bytes_read": 8192},
        "auto_disabled_capabilities": [],
    }
    store.apply_diagnostic_results([], started_at=1, finished_at=2, platforms=[platform])
    latest = store.get_latest_diagnostics()["capability"]
    assert latest["platforms"][0]["source"] == "arxiv"
    assert latest["platforms"][0]["abstract"]["abstract_length"] == 100


def test_diagnostic_whitelist_drops_unknown_secret_fields_from_items_and_summary(isolated):
    store.save_diagnostic_run(
        "capability", 1, 2,
        {"count": 1, "statuses": {"ok": 1}, "Authorization": "secret"},
        [{"source": "arxiv", "status": "ok", "Authorization": "secret",
          "raw_body": "full abstract body", "pdf_bytes": "%PDF-secret"}],
    )
    conn = sqlite3.connect(store._DB_PATH)
    try:
        summary = conn.execute(
            "SELECT summary_json FROM paper_search_diagnostic_runs"
        ).fetchone()[0]
        item = conn.execute(
            "SELECT payload_json FROM paper_search_diagnostic_items"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "secret" not in summary + item
    assert "raw_body" not in item and "pdf_bytes" not in item


def test_unsupported_capability_cannot_be_toggled(isolated):
    policy = store.get_paper_search_policy()
    with pytest.raises(store.PaperSearchSettingsError, match="不支持"):
        store.set_source_capability(
            "doi", "fulltext", False,
            expected_version=policy.version, updated_by="administrator",
        )


def test_unpaywall_diagnostic_close_success_recheck_and_manual_recovery(isolated):
    store.apply_diagnostic_results([
        {"source": "unpaywall", "capability": "fulltext", "status": "failed",
         "latency_ms": 123, "error_code": "http_503", "message": "service unavailable",
         "auto_disable": True},
    ], started_at=1, finished_at=2)
    closed = store.get_paper_search_policy()
    state = closed.capabilities["unpaywall"]["fulltext"]
    assert state["enabled"] is False
    assert state["disabled_by"] == "diagnostic"
    assert state["reason_code"] == "http_503"

    store.apply_diagnostic_results([
        {"source": "unpaywall", "capability": "fulltext", "status": "ok",
         "latency_ms": 50, "kb_per_second": 400, "auto_disable": False},
    ], started_at=3, finished_at=4)
    rechecked = store.get_paper_search_policy()
    state = rechecked.capabilities["unpaywall"]["fulltext"]
    assert state["enabled"] is False
    assert state["disabled_by"] == "diagnostic"
    assert state["last_diagnostic_status"] == "ok"
    assert state["last_kb_per_second"] == 400

    recovered = store.set_source_capability(
        "unpaywall", "fulltext", True,
        expected_version=rechecked.version, updated_by="administrator",
    )
    state = recovered.capabilities["unpaywall"]["fulltext"]
    assert state["enabled"] is True
    assert state["disabled_by"] is None
    assert state["reason"] is None
    assert state["last_diagnostic_status"] == "ok"
    assert state["last_kb_per_second"] == 400


def test_last_complete_timestamp_ignores_single_capability_runs(isolated):
    store.apply_diagnostic_results(
        [], started_at=1, finished_at=2,
        summary_metadata={"diagnostic_scope": "complete_all", "requested_sources": []},
    )
    store.apply_diagnostic_results(
        [], started_at=3, finished_at=4,
        summary_metadata={
            "diagnostic_scope": "complete_platforms",
            "requested_sources": ["arxiv"],
        },
    )
    store.apply_diagnostic_results(
        [], started_at=5, finished_at=6,
        summary_metadata={
            "diagnostic_scope": "single_capability",
            "requested_sources": ["arxiv"],
        },
    )
    latest = store.get_latest_diagnostics()
    assert latest["capability"]["finished_at"] == 6
    assert latest["last_complete_at"] == 4


def test_repeat_diagnostic_failure_refreshes_reason_without_manual_override(isolated):
    store.apply_diagnostic_results([
        {"source": "arxiv", "capability": "abstract", "status": "failed",
         "error_code": "first_failure", "message": "first reason",
         "auto_disable": True},
    ], started_at=1, finished_at=2)
    first = store.get_paper_search_policy()
    first_disabled_at = first.capabilities["arxiv"]["abstract"]["disabled_at"]

    store.apply_diagnostic_results([
        {"source": "arxiv", "capability": "abstract", "status": "failed",
         "error_code": "second_failure", "message": "second reason",
         "auto_disable": True},
    ], started_at=3, finished_at=4)
    repeated = store.get_paper_search_policy()
    state = repeated.capabilities["arxiv"]["abstract"]
    assert state["enabled"] is False
    assert state["disabled_by"] == "diagnostic"
    assert state["reason_code"] == "second_failure"
    assert state["reason"] == "second reason"
    assert state["disabled_at"] == first_disabled_at

    recovered = store.set_source_capability(
        "arxiv", "abstract", True,
        expected_version=repeated.version, updated_by="administrator",
    )
    manually_closed = store.set_source_capability(
        "arxiv", "abstract", False,
        expected_version=recovered.version, updated_by="administrator",
    )
    store.apply_diagnostic_results([
        {"source": "arxiv", "capability": "abstract", "status": "failed",
         "error_code": "third_failure", "message": "third reason",
         "auto_disable": True},
    ], started_at=5, finished_at=6)
    state = store.get_paper_search_policy().capabilities["arxiv"]["abstract"]
    assert state["disabled_by"] == "administrator"
    assert state["reason_code"] == "administrator_disabled"
    assert state["reason"] == manually_closed.capabilities["arxiv"]["abstract"]["reason"]
