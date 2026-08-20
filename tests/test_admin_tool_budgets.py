"""Admin endpoints: per-tool budgets, tool-breaker recovery and channel suggestions."""
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
import core.tool_budget_store as budget_store
import core.user_store as user_store
from app.api.v1 import admin as admin_api


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "users.db"
    monkeypatch.setattr(auth_store, "_DB_PATH", db)
    monkeypatch.setattr(paper_store, "_DB_PATH", db)
    monkeypatch.setattr(user_store, "_DB_PATH", db)
    monkeypatch.setattr(budget_store, "_DB_PATH", db)
    monkeypatch.setattr(paper_store, "_seed", lambda: paper_store.PaperSearchPolicy(
        sources={name: True for name in paper_store.SOURCE_IDS}))
    auth_store.reset_cache(); paper_store.reset_cache(); budget_store.reset_cache()
    auth_store.update_auth_settings(
        {"auth_required": True}, expected_version=auth_store.get_auth_settings().version,
        updated_by="test")
    admin, _ = user_store.bootstrap_administrator("administrator", "", "admin-password")
    normal = user_store.create_user("normal", "normal-password")
    yield {
        "admin": f"Bearer {user_store.issue_token(admin['id'])}",
        "normal": f"Bearer {user_store.issue_token(normal['id'])}",
    }
    auth_store.reset_cache(); paper_store.reset_cache(); budget_store.reset_cache()


def test_tool_budgets_are_admin_only(env):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.get_admin_tool_budgets(env["normal"]))
    assert exc.value.status_code == 403


def test_catalog_is_rich_and_covers_all_tools(env):
    data = asyncio.run(admin_api.get_admin_tool_budgets(env["admin"]))
    names = {item["name"] for item in data["catalog"]}
    from agents.chat_tools import ARGS_SCHEMAS
    assert "use_skill" not in names
    assert set(ARGS_SCHEMAS) - {"use_skill"} <= names
    for item in data["catalog"]:
        assert item["label"]
        assert item["description"]
        assert item["recommended_max"] >= 5
        assert item["current_seconds"] == item["default_seconds"]
        assert item["overridden"] is False
    assert data["policy"]["reserve_seconds"] == 8.0
    assert data["limits"]["api_turn_soft_seconds"] == 95


def test_update_is_immediate_with_conflict_and_unknown_tool(env):
    data = asyncio.run(admin_api.get_admin_tool_budgets(env["admin"]))
    version = data["policy"]["version"]
    result = asyncio.run(admin_api.put_admin_tool_budgets(
        admin_api.ToolBudgetPolicyUpdate(
            expected_version=version, budgets={"search_papers": 60},
            reserve_seconds=10),
        env["admin"]))
    assert result["policy"]["budgets"]["search_papers"] == 60
    assert budget_store.get_tool_budget("search_papers") == 60.0
    assert budget_store.get_turn_reserve() == 10.0

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.put_admin_tool_budgets(
            admin_api.ToolBudgetPolicyUpdate(
                expected_version=version, budgets={"search_papers": 61}),
            env["admin"]))
    assert exc.value.status_code == 409

    fresh = asyncio.run(admin_api.get_admin_tool_budgets(env["admin"]))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.put_admin_tool_budgets(
            admin_api.ToolBudgetPolicyUpdate(
                expected_version=fresh["policy"]["version"],
                budgets={"made_up_tool": 60}),
            env["admin"]))
    assert exc.value.status_code == 422


def test_tool_breaker_recovery(env, monkeypatch):
    from core.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker()
    monkeypatch.setattr("core.circuit_breaker.get_breaker", lambda: breaker)
    for _ in range(3):
        breaker.record_failure("search_papers")

    data = asyncio.run(admin_api.get_admin_tool_budgets(env["admin"]))
    assert data["breaker_states"]["search_papers"]["state"] == "open"
    result = admin_api.post_tool_breaker_recover(
        admin_api.ToolBreakerRecoverRequest(tool="search_papers"), env["admin"])
    assert result["breaker_states"]["search_papers"]["state"] == "closed"


def test_old_policy_table_migrates_api_turn_soft_seconds(env, monkeypatch, tmp_path):
    import sqlite3

    legacy = tmp_path / "legacy-users.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute("""CREATE TABLE tool_budget_policy (
            id INTEGER PRIMARY KEY, budgets_json TEXT NOT NULL,
            default_budget_seconds REAL NOT NULL, reserve_seconds REAL NOT NULL,
            version INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("INSERT INTO tool_budget_policy VALUES (1, '{}', 30, 8, 3, 'legacy', 1)")
        conn.commit()
    monkeypatch.setattr(budget_store, "_DB_PATH", legacy)
    budget_store.reset_cache()
    policy = budget_store.get_tool_budget_policy()
    assert policy.api_turn_soft_seconds == 95
    with sqlite3.connect(legacy) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_budget_policy)")}
    assert "api_turn_soft_seconds" in columns


def test_api_turn_soft_seconds_update_and_bounds(env):
    data = asyncio.run(admin_api.get_admin_tool_budgets(env["admin"]))
    result = asyncio.run(admin_api.put_admin_tool_budgets(
        admin_api.ToolBudgetPolicyUpdate(
            expected_version=data["policy"]["version"], api_turn_soft_seconds=88),
        env["admin"],
    ))
    assert result["policy"]["api_turn_soft_seconds"] == 88
    with pytest.raises(Exception):
        admin_api.ToolBudgetPolicyUpdate(
            expected_version=result["policy"]["version"], api_turn_soft_seconds=101)
