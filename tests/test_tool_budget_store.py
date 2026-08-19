"""Tool-budget policy store: defaults, overrides, optimistic lock, validation."""
from __future__ import annotations

import pytest

import core.tool_budget_store as store


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "users.db")
    store.reset_cache()
    yield
    store.reset_cache()


def test_defaults_match_code_budgets(isolated):
    policy = store.get_tool_budget_policy()
    assert policy.budgets == {}
    assert policy.default_budget_seconds == 30.0
    assert policy.reserve_seconds == 8.0
    assert store.get_tool_budget("search_papers") == 45.0
    assert store.get_tool_budget("deep_read") == 75.0
    assert store.get_tool_budget("write_review") == 60.0
    assert store.get_tool_budget("ask_papers") == 30.0
    assert store.get_tool_budget("brand_new_tool") == 30.0
    assert store.get_turn_reserve() == 8.0


def test_update_overrides_and_optimistic_lock(isolated):
    current = store.get_tool_budget_policy()
    updated = store.update_tool_budget_policy(
        {"budgets": {"search_papers": 60}, "reserve_seconds": 10},
        expected_version=current.version, updated_by="admin",
    )
    assert updated.budgets["search_papers"] == 60.0
    assert updated.reserve_seconds == 10.0
    assert updated.version == current.version + 1
    assert store.get_tool_budget("search_papers") == 60.0
    # Sibling tools keep their code defaults; full-dict replacement semantics.
    assert store.get_tool_budget("research_map") == 45.0
    with pytest.raises(store.ToolBudgetVersionConflict):
        store.update_tool_budget_policy(
            {"budgets": {}}, expected_version=current.version, updated_by="stale")


def test_validation_ranges(isolated):
    current = store.get_tool_budget_policy()
    with pytest.raises(store.ToolBudgetSettingsError, match="预算必须"):
        store.update_tool_budget_policy(
            {"budgets": {"search_papers": 200}},
            expected_version=current.version, updated_by="admin")
    with pytest.raises(store.ToolBudgetSettingsError, match="默认预算"):
        store.update_tool_budget_policy(
            {"default_budget_seconds": 2},
            expected_version=current.version, updated_by="admin")
    with pytest.raises(store.ToolBudgetSettingsError, match="预留量"):
        store.update_tool_budget_policy(
            {"reserve_seconds": 40},
            expected_version=current.version, updated_by="admin")
    with pytest.raises(store.ToolBudgetSettingsError, match="未知设置字段"):
        store.update_tool_budget_policy(
            {"nope": 1}, expected_version=current.version, updated_by="admin")


def test_accessor_falls_back_to_code_defaults_on_storage_failure(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_tool_budget_policy", boom)
    assert store.get_tool_budget("search_papers") == 45.0
    assert store.get_tool_budget("unknown") == 30.0
    assert store.get_turn_reserve() == 8.0
