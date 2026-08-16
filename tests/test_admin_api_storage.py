"""Module 6 administrator API storage policy and safety flows."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import core.user_store as user_store
from app.api.v1 import admin as admin_api
from app.core.config import settings


@pytest.fixture
def admin_env(tmp_path, monkeypatch):
    monkeypatch.setattr(user_store, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setenv("OPENAI_API_STORAGE_ROOT", str(tmp_path / "openai-api"))
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "guest_access", False)
    admin, _ = user_store.bootstrap_administrator(
        "administrator", "administrator@example.com", "admin-password"
    )
    normal = user_store.create_user("normal", "normal-password")
    return {
        "admin": f"Bearer {user_store.issue_token(admin['id'])}",
        "normal": f"Bearer {user_store.issue_token(normal['id'])}",
    }


def test_storage_admin_requires_administrator(admin_env):
    with pytest.raises(HTTPException) as exc:
        admin_api.get_api_storage_policy(admin_env["normal"])
    assert exc.value.status_code == 403


def test_policy_help_catalog_covers_every_required_option(admin_env):
    response = admin_api.get_api_storage_policy(admin_env["admin"])
    keys = set(response["help"]["items"])
    assert {
        "preset.privacy", "preset.balanced", "preset.performance", "ttl",
        "thresholds", "critical.pause_heavy", "critical.emergency_evict",
        "trace.off", "trace.metadata", "trace.full", "cleanup",
    } <= keys
    required = {"title", "does", "affected", "benefits", "drawbacks", "privacy",
                "disk", "latency_cost", "continuity", "effective", "fallback", "restore"}
    assert all(required <= set(item) for item in response["help"]["items"].values())
    assert "openai-api" not in repr(response).lower()


def test_safe_policy_update_and_optimistic_conflict(admin_env):
    current = admin_api.get_api_storage_policy(admin_env["admin"])["policy"]
    body = admin_api.ApiStoragePolicyUpdate(
        expected_version=current["version"], cleanup_interval_minutes=120,
    )
    updated = admin_api.put_api_storage_policy(body, admin_env["admin"])["policy"]
    assert updated["cleanup_interval_minutes"] == 120
    with pytest.raises(HTTPException) as conflict:
        admin_api.put_api_storage_policy(body, admin_env["admin"])
    assert conflict.value.status_code == 409


def test_dangerous_policy_requires_matching_one_time_preview(admin_env):
    current = admin_api.get_api_storage_policy(admin_env["admin"])["policy"]
    proposed = {"trace_mode": "full"}
    body = admin_api.ApiStoragePolicyUpdate(
        expected_version=current["version"], trace_mode="full",
    )
    with pytest.raises(HTTPException) as required:
        admin_api.put_api_storage_policy(body, admin_env["admin"])
    assert required.value.status_code == 409
    preview = admin_api.post_api_storage_cleanup_preview(
        admin_api.CleanupPreviewRequest(action="policy_update", proposed=proposed),
        admin_env["admin"],
    )
    confirmed = admin_api.ApiStoragePolicyUpdate(
        expected_version=current["version"], trace_mode="full",
        preview_token=preview["token"],
    )
    assert admin_api.put_api_storage_policy(confirmed, admin_env["admin"])["policy"]["trace_mode"] == "full"
    with pytest.raises(HTTPException):
        admin_api.put_api_storage_policy(confirmed, admin_env["admin"])


def test_policy_validation_rejects_bad_cross_thresholds_and_hard_stop(admin_env):
    current = admin_api.get_api_storage_policy(admin_env["admin"])["policy"]
    with pytest.raises(HTTPException) as exc:
        admin_api.put_api_storage_policy(
            admin_api.ApiStoragePolicyUpdate(
                expected_version=current["version"], observe_threshold_percent=90,
                pressure_threshold_percent=80,
            ),
            admin_env["admin"],
        )
    assert exc.value.status_code == 422
    with pytest.raises(ValidationError):
        admin_api.ApiStoragePolicyUpdate(
            expected_version=current["version"], hard_stop_threshold_percent=99,
        )


def test_usage_status_runs_and_cleanup_response_hide_sensitive_data(admin_env):
    usage = admin_api.get_api_storage_usage(admin_env["admin"])
    status = admin_api.get_api_storage_status(admin_env["admin"])
    runs = admin_api.get_api_storage_cleanup_runs(admin_env["admin"])
    combined = repr((usage, status, runs))
    assert "password" not in combined.lower()
    assert "authorization" not in combined.lower()
    assert "relative_path" not in combined
    assert status["text_chat_allowed"] is True


def test_immediate_cleanup_preview_execute_and_legacy_scan(admin_env):
    preview = admin_api.post_api_storage_cleanup_preview(
        admin_api.CleanupPreviewRequest(action="immediate_cleanup"), admin_env["admin"]
    )
    result = admin_api.post_api_storage_cleanup_execute(
        admin_api.CleanupExecuteRequest(token=preview["token"]), admin_env["admin"]
    )
    assert result["result"]["mode"].startswith("execute:")

    legacy = admin_api.post_api_storage_cleanup_preview(
        admin_api.CleanupPreviewRequest(action="legacy_scan"), admin_env["admin"]
    )
    scanned = admin_api.post_api_storage_legacy_scan(
        admin_api.CleanupExecuteRequest(token=legacy["token"]), admin_env["admin"]
    )
    assert scanned["result"]["mode"] == "reconcile"
