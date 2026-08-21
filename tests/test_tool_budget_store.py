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
    assert policy.api_turn_soft_seconds == 95.0
    assert policy.api_turn_hard_seconds == 105.0
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
    # 默认硬时限 105 下，软时限必须 ≤ 100。
    with pytest.raises(store.ToolBudgetSettingsError, match="软时限"):
        store.update_tool_budget_policy(
            {"api_turn_soft_seconds": 101},
            expected_version=current.version, updated_by="admin")


def test_api_turn_hard_and_soft_cross_validation(isolated):
    current = store.get_tool_budget_policy()
    # 硬时限无固定上限；抬高后软时限可超过旧的 100 秒上限。
    updated = store.update_tool_budget_policy(
        {"api_turn_hard_seconds": 200}, expected_version=current.version,
        updated_by="admin")
    assert updated.api_turn_hard_seconds == 200.0
    updated = store.update_tool_budget_policy(
        {"api_turn_soft_seconds": 150}, expected_version=updated.version,
        updated_by="admin")
    assert updated.api_turn_soft_seconds == 150.0
    # 逐工具/默认预算上限跟随硬时限：硬 200 时预算 150 合法。
    updated = store.update_tool_budget_policy(
        {"budgets": {"deep_read": 150}, "default_budget_seconds": 120},
        expected_version=updated.version, updated_by="admin")
    assert updated.budgets["deep_read"] == 150.0
    # 软时限必须比硬时限至少小 5 秒。
    with pytest.raises(store.ToolBudgetSettingsError, match="软时限"):
        store.update_tool_budget_policy(
            {"api_turn_soft_seconds": 196}, expected_version=updated.version,
            updated_by="admin")
    # 硬时限最小 35（软最小 30 + 5 收尾余量）。
    with pytest.raises(store.ToolBudgetSettingsError, match="硬时限"):
        store.update_tool_budget_policy(
            {"api_turn_hard_seconds": 34}, expected_version=updated.version,
            updated_by="admin")
    # 调低硬时限会让超出新上限的存量预算失效，保存被拒并指明工具。
    with pytest.raises(store.ToolBudgetSettingsError, match="deep_read"):
        store.update_tool_budget_policy(
            {"api_turn_hard_seconds": 60}, expected_version=updated.version,
            updated_by="admin")


def test_accessor_falls_back_to_code_defaults_on_storage_failure(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_tool_budget_policy", boom)
    assert store.get_tool_budget("search_papers") == 45.0
    assert store.get_tool_budget("unknown") == 30.0
    assert store.get_turn_reserve() == 8.0
