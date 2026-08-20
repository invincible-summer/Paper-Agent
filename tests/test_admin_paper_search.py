from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import core.auth_settings_store as auth_store
import core.paper_search_settings_store as paper_store
import core.user_store as user_store
from app.api.v1 import admin as admin_api


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "users.db"
    monkeypatch.setattr(auth_store, "_DB_PATH", db)
    monkeypatch.setattr(paper_store, "_DB_PATH", db)
    monkeypatch.setattr(user_store, "_DB_PATH", db)
    monkeypatch.setattr(paper_store, "_seed", lambda: paper_store.PaperSearchPolicy(
        sources={name: True for name in paper_store.SOURCE_IDS}))
    auth_store.reset_cache(); paper_store.reset_cache()
    auth_store.update_auth_settings(
        {"auth_required": True}, expected_version=auth_store.get_auth_settings().version,
        updated_by="test")
    admin, _ = user_store.bootstrap_administrator("administrator", "", "admin-password")
    normal = user_store.create_user("normal", "normal-password")
    yield {"admin": f"Bearer {user_store.issue_token(admin['id'])}",
           "normal": f"Bearer {user_store.issue_token(normal['id'])}"}
    auth_store.reset_cache(); paper_store.reset_cache()



def test_legacy_separate_connectivity_and_download_routes_are_removed():
    paths = {route.path for route in admin_api.router.routes}
    assert "/admin/paper-search/diagnostics/connectivity" not in paths
    assert "/admin/paper-search/diagnostics/download" not in paths
    assert "/admin/paper-search/diagnostics/capabilities" in paths


def test_latest_diagnostics_exposes_only_unified_capability_kind(env):
    payload = asyncio.run(
        admin_api.get_paper_search_diagnostics_latest(env["admin"])
    )
    assert set(payload) == {"capability", "recent", "last_complete_at"}

def test_policy_payload_exposes_capability_matrix_without_source_breaker(env):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.get_admin_paper_search_policy(env["normal"]))
    assert exc.value.status_code == 403
    payload = asyncio.run(admin_api.get_admin_paper_search_policy(env["admin"]))
    assert "capabilities" in payload
    assert set(payload["capabilities"]["arxiv"]) == {"search", "abstract", "fulltext"}
    assert "runtime_status" not in payload
    assert "breaker" not in payload
    catalog = {item["id"]: item for item in payload["source_catalog"]}
    assert {"unpaywall", "doi"} <= set(catalog)
    assert catalog["doi"]["supports_search"] is False
    assert payload["configuration_status"]["unpaywall"] in {"ready", "missing_contact_email"}
    assert payload["disabled_by"]["arxiv"]["search"] is None
    assert payload["disabled_reason"]["arxiv"]["search"] is None


def test_single_capability_toggle_is_immediate_and_optimistically_locked(env):
    current = asyncio.run(admin_api.get_admin_paper_search_policy(env["admin"]))
    version = current["policy"]["version"]
    result = asyncio.run(admin_api.put_paper_source_capability(
        "arxiv", "abstract",
        admin_api.PaperCapabilityUpdate(expected_version=version, enabled=False), env["admin"]))
    assert result["policy"]["capabilities"]["arxiv"]["abstract"]["enabled"] is False
    assert result["policy"]["sources"]["arxiv"] is True
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.put_paper_source_capability(
            "arxiv", "abstract",
            admin_api.PaperCapabilityUpdate(expected_version=version, enabled=True), env["admin"]))
    assert exc.value.status_code == 409


def test_full_diagnostic_can_force_test_disabled_source_and_auto_close(env, monkeypatch):
    current = paper_store.get_paper_search_policy()
    paper_store.set_source_capability(
        "arxiv", "search", False, expected_version=current.version, updated_by="admin")

    async def fake_run(sources, capability):
        assert sources == ["arxiv"] and capability is None
        return [{
            "source": "arxiv",
            "connectivity": {"status": "ok", "latency_ms": 20, "error_code": None, "message": None},
            "search": {"status": "ok", "latency_ms": 20, "result_count": 1, "error_code": None, "message": None},
            "abstract": {"status": "failed", "latency_ms": 20, "error_code": "abstract_empty", "message": "摘要为空"},
            "fulltext": {"status": "slow", "latency_ms": 1000, "bytes_read": 1024,
                         "kb_per_second": 1, "pdf_magic_valid": True, "error_code": None, "message": "慢"},
            "auto_disabled_capabilities": ["abstract"],
        }]
    monkeypatch.setattr("tools.search.diagnostics.run_platform_diagnostics", fake_run)
    result = asyncio.run(admin_api.post_paper_capability_diagnostics(
        admin_api.PaperSearchDiagnosticRequest(sources=["arxiv"]), env["admin"]))
    assert result["platforms"][0]["search"]["status"] == "ok"
    policy = paper_store.get_paper_search_policy()
    assert policy.capabilities["arxiv"]["search"]["enabled"] is False  # success never recovers
    assert policy.capabilities["arxiv"]["abstract"]["enabled"] is False
    assert policy.capabilities["arxiv"]["fulltext"]["enabled"] is True  # slow never closes


def test_diagnostic_semaphore_rejects_duplicate_batch(env, monkeypatch):
    async def slow(*_args):
        await asyncio.sleep(.1); return []
    monkeypatch.setattr("tools.search.diagnostics.run_platform_diagnostics", slow)
    async def run_two():
        first = asyncio.create_task(admin_api.post_paper_capability_diagnostics(
            admin_api.PaperSearchDiagnosticRequest(sources=["arxiv"]), env["admin"]))
        await asyncio.sleep(.01)
        with pytest.raises(HTTPException) as exc:
            await admin_api.post_paper_capability_diagnostics(
                admin_api.PaperSearchDiagnosticRequest(sources=["crossref"]), env["admin"])
        await first
        return exc.value.status_code
    assert asyncio.run(run_two()) == 409


def test_unsupported_doi_capability_toggle_is_rejected(env):
    payload = asyncio.run(admin_api.get_admin_paper_search_policy(env["admin"]))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.put_paper_source_capability(
            "doi", "fulltext",
            admin_api.PaperCapabilityUpdate(
                expected_version=payload["policy"]["version"], enabled=False,
            ),
            env["admin"],
        ))
    assert exc.value.status_code == 422


def test_policy_last_checked_at_tracks_complete_platform_not_single_capability(env, monkeypatch):
    async def fake_run(sources, capability):
        source = (sources or ["arxiv"])[0]
        return [{
            "source": source,
            "connectivity": {"status": "ok", "latency_ms": 1},
            "search": {"status": "not_applicable", "latency_ms": None},
            "abstract": {"status": "not_applicable", "latency_ms": None},
            "fulltext": {"status": "not_applicable", "latency_ms": None},
            "auto_disabled_capabilities": [],
            "_persist_capabilities": [],
        }]

    monkeypatch.setattr("tools.search.diagnostics.run_platform_diagnostics", fake_run)
    asyncio.run(admin_api.post_paper_capability_diagnostics(
        admin_api.PaperSearchDiagnosticRequest(sources=["arxiv"]),
        env["admin"],
    ))
    payload = asyncio.run(admin_api.get_admin_paper_search_policy(env["admin"]))
    complete_at = payload["last_checked_at"]
    assert complete_at is not None

    asyncio.run(admin_api.post_paper_capability_diagnostics(
        admin_api.PaperSearchDiagnosticRequest(
            sources=["arxiv"], capability="connectivity",
        ),
        env["admin"],
    ))
    payload = asyncio.run(admin_api.get_admin_paper_search_policy(env["admin"]))
    assert payload["last_checked_at"] == complete_at
