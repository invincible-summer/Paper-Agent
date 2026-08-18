from __future__ import annotations

import pytest

from core import runtime_performance_policy as store


def test_policy_initializes_defaults_and_optimistic_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "users.db")
    store.reset_performance_policy_cache()
    current = store.get_performance_policy()
    assert current.startup_prewarm_mode == "blocking"
    assert current.map_citation_mode == "fast"
    updated = store.update_performance_policy(
        {"startup_prewarm_mode": "role_first", "map_citation_mode": "off"},
        expected_version=current.version,
        updated_by="admin",
    )
    assert updated.version == current.version + 1
    assert updated.startup_prewarm_mode == "role_first"
    with pytest.raises(store.PerformancePolicyVersionConflict):
        store.update_performance_policy(
            {"map_citation_mode": "quality"}, expected_version=current.version, updated_by="admin")


def test_policy_rejects_unknown_enum(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "users.db")
    store.reset_performance_policy_cache()
    current = store.get_performance_policy()
    with pytest.raises(store.PerformancePolicyError):
        store.update_performance_policy(
            {"map_citation_mode": "slow"}, expected_version=current.version, updated_by="admin")
