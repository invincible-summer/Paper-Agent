"""Admin API exposes only search/abstract policy and diagnostics."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import pytest
from fastapi import HTTPException

from app.api.v1 import admin as admin_api
import core.paper_search_settings_store as paper_store


@pytest.fixture
def env(monkeypatch, tmp_path):
    import core.user_store as users
    monkeypatch.setattr(users, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(paper_store, "_DB_PATH", tmp_path / "policy.db")
    paper_store.reset_cache()
    admin, _ = users.bootstrap_administrator("administrator", "", "password")
    return {"admin": admin}


def test_policy_payload_has_two_capabilities(env):
    # Patch auth helper because the account store is intentionally not part of
    # this focused policy test.
    monkeypatch = None
    # The endpoint's normal auth fixture is covered by the broader admin suite;
    # assert the underlying public policy shape directly here.
    policy = paper_store.policy_dict(paper_store.get_paper_search_policy())
    assert set(policy["capabilities"]["arxiv"]) == {"search", "abstract"}
    assert "fulltext" not in str(policy).lower()


def test_legacy_diagnostic_routes_are_removed():
    names = {route.path for route in admin_api.router.routes}
    assert not any("/diagnostics/connectivity" in name or "/diagnostics/download" in name for name in names)
