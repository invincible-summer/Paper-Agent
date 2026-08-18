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
    yield {
        "admin": f"Bearer {user_store.issue_token(admin['id'])}",
        "normal": f"Bearer {user_store.issue_token(normal['id'])}",
    }
    auth_store.reset_cache(); paper_store.reset_cache()


def test_policy_admin_only_and_update_is_immediate(env):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.get_admin_paper_search_policy(env["normal"]))
    assert exc.value.status_code == 403
    current = asyncio.run(admin_api.get_admin_paper_search_policy(env["admin"]))
    version = current["policy"]["version"]
    result = asyncio.run(admin_api.put_admin_paper_search_policy(
        admin_api.PaperSearchPolicyUpdate(
            expected_version=version, sources={"openalex": False},
            paper_fetch_mode="probe_only", fetch_policy_disclosure="silent"),
        env["admin"],
    ))
    assert result["policy"]["sources"]["openalex"] is False
    assert paper_store.get_paper_search_policy().paper_fetch_mode == "probe_only"


def test_policy_conflict_returns_409(env):
    version = paper_store.get_paper_search_policy().version
    asyncio.run(admin_api.put_admin_paper_search_policy(
        admin_api.PaperSearchPolicyUpdate(expected_version=version, verify_fulltext=False),
        env["admin"]))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.put_admin_paper_search_policy(
            admin_api.PaperSearchPolicyUpdate(expected_version=version, verify_fulltext=True),
            env["admin"]))
    assert exc.value.status_code == 409


def test_connectivity_can_test_disabled_source(env, monkeypatch):
    version = paper_store.get_paper_search_policy().version
    asyncio.run(admin_api.put_admin_paper_search_policy(
        admin_api.PaperSearchPolicyUpdate(expected_version=version,
                                          sources={"openalex": False}), env["admin"]))
    async def fake_run(sources):
        assert sources == ["openalex"]
        return [{"source": "openalex", "status": "rate_limited", "http_status": 429}]
    monkeypatch.setattr("tools.search.diagnostics.run_connectivity", fake_run)
    result = asyncio.run(admin_api.post_paper_search_connectivity(
        admin_api.PaperSearchDiagnosticRequest(sources=["openalex"]), env["admin"]))
    assert result["items"][0]["http_status"] == 429
    assert paper_store.get_paper_search_policy().sources["openalex"] is False


def test_download_diagnostic_does_not_accept_unknown_target(env, monkeypatch):
    async def fake_run(_sources):
        raise ValueError("未知检测目标：evil")
    monkeypatch.setattr("tools.search.diagnostics.run_download_speed", fake_run)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.post_paper_search_download_test(
            admin_api.PaperSearchDiagnosticRequest(sources=["evil"]), env["admin"]))
    assert exc.value.status_code == 422
