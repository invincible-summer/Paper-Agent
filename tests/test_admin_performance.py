from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import core.auth_settings_store as auth_store
import core.paper_search_settings_store as paper_store
import core.runtime_performance_policy as performance_store
import core.user_store as user_store
from app.api.v1 import admin as admin_api


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "users.db"
    for module in (auth_store, paper_store, performance_store, user_store):
        monkeypatch.setattr(module, "_DB_PATH", db)
    auth_store.reset_cache(); paper_store.reset_cache(); performance_store.reset_cache()
    admin, _ = user_store.bootstrap_administrator("administrator", "", "admin-password")
    normal = user_store.create_user("normal", "normal-password")
    yield {
        "admin": f"Bearer {user_store.issue_token(admin['id'])}",
        "normal": f"Bearer {user_store.issue_token(normal['id'])}",
    }
    auth_store.reset_cache(); paper_store.reset_cache(); performance_store.reset_cache()


def test_performance_policy_is_admin_only_and_versioned(env):
    with pytest.raises(HTTPException) as exc:
        admin_api.get_performance_policy_api(env["normal"])
    assert exc.value.status_code == 403
    current = admin_api.get_performance_policy_api(env["admin"])
    version = current["settings"]["version"]
    updated = admin_api.put_performance_policy_api(
        admin_api.PerformancePolicyUpdate(
            expected_version=version, startup_prewarm_mode="role_first",
            map_citation_mode="quality"), env["admin"])
    assert updated["settings"]["startup_prewarm_mode"] == "role_first"
    assert updated["restart_required"] is True
    with pytest.raises(HTTPException) as stale:
        admin_api.put_performance_policy_api(
            admin_api.PerformancePolicyUpdate(
                expected_version=version, map_citation_mode="off"), env["admin"])
    assert stale.value.status_code == 409


def test_openalex_disabled_forces_effective_citation_off(env):
    current = paper_store.get_paper_search_policy()
    paper_store.update_paper_search_policy(
        {"sources": {**current.sources, "openalex": False}},
        expected_version=current.version, updated_by="test")
    response = admin_api.get_performance_policy_api(env["admin"])
    assert response["openalex_enabled"] is False
    assert response["effective_map_citation_mode"] == "off"
    assert response["map_citation_disabled_reason"]
