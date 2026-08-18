"""Admin auth-settings endpoints: optimistic lock, SMTP guard, danger confirm."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import core.auth_settings_store as ass  # noqa: E402
import core.user_store as us  # noqa: E402
from app.api.v1 import admin as admin_api  # noqa: E402
from app.api.v1 import auth as auth_api  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ass, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(us, "_DB_PATH", tmp_path / "users.db")
    ass.reset_cache()
    ass.update_auth_settings(
        {"auth_required": True, "guest_access": True, "registration_open": True},
        expected_version=ass.get_auth_settings().version, updated_by="test")
    admin, _ = us.bootstrap_administrator("administrator", "", "admin-password")
    normal = us.create_user("normal", "normal-password")
    return {
        "admin": f"Bearer {us.issue_token(admin['id'])}",
        "normal": f"Bearer {us.issue_token(normal['id'])}",
    }


def test_get_returns_settings_and_smtp_status(env):
    data = admin_api.get_auth_settings(env["admin"])
    assert data["settings"]["auth_required"] is True
    assert data["settings"]["guest_access"] is True
    assert data["settings"]["version"] >= 1
    assert data["smtp"]["configured"] is False


def test_non_admin_is_forbidden(env):
    with pytest.raises(HTTPException) as exc:
        admin_api.get_auth_settings(env["normal"])
    assert exc.value.status_code == 403


def test_put_updates_flags_and_takes_effect_immediately(env):
    version = admin_api.get_auth_settings(env["admin"])["settings"]["version"]
    result = admin_api.put_auth_settings(
        admin_api.AuthSettingsUpdate(expected_version=version, guest_access=False),
        env["admin"])
    assert result["settings"]["guest_access"] is False
    with pytest.raises(HTTPException) as guest:
        auth_api.current_user(None, "browser-abc-123")
    assert guest.value.status_code == 401


def test_put_version_conflict_returns_409(env):
    admin_api.get_auth_settings(env["admin"])
    stale = ass.get_auth_settings().version
    admin_api.put_auth_settings(
        admin_api.AuthSettingsUpdate(expected_version=stale, registration_open=False),
        env["admin"])
    with pytest.raises(HTTPException) as exc:
        admin_api.put_auth_settings(
            admin_api.AuthSettingsUpdate(expected_version=stale, guest_access=False),
            env["admin"])
    assert exc.value.status_code == 409


def test_verify_requirement_needs_smtp(env, monkeypatch):
    version = ass.get_auth_settings().version
    with pytest.raises(HTTPException) as exc:
        admin_api.put_auth_settings(
            admin_api.AuthSettingsUpdate(
                expected_version=version, email_requirement="verify"),
            env["admin"])
    assert exc.value.status_code == 422

    monkeypatch.setattr("core.email_sender.smtp_configured", lambda: True)
    result = admin_api.put_auth_settings(
        admin_api.AuthSettingsUpdate(
            expected_version=version, email_requirement="verify"),
        env["admin"])
    assert result["settings"]["email_requirement"] == "verify"


def test_disabling_auth_requires_explicit_confirmation(env):
    version = ass.get_auth_settings().version
    with pytest.raises(HTTPException) as exc:
        admin_api.put_auth_settings(
            admin_api.AuthSettingsUpdate(expected_version=version, auth_required=False),
            env["admin"])
    assert exc.value.status_code == 422

    result = admin_api.put_auth_settings(
        admin_api.AuthSettingsUpdate(
            expected_version=version, auth_required=False, confirm_disable_auth=True),
        env["admin"])
    assert result["settings"]["auth_required"] is False
    assert auth_api.current_user(None, None)["id"] == "local"


def test_test_email_sends_and_validates_address(env, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("core.email_sender.smtp_configured", lambda: True)
    monkeypatch.setattr("core.email_sender.send_test_email", lambda to: sent.append(to))

    assert admin_api.post_auth_settings_test_email(
        admin_api.AuthTestEmailRequest(to="you@example.com"), env["admin"]
    ) == {"status": "sent"}
    assert sent == ["you@example.com"]

    with pytest.raises(HTTPException) as exc:
        admin_api.post_auth_settings_test_email(
            admin_api.AuthTestEmailRequest(to="not-an-email"), env["admin"])
    assert exc.value.status_code == 422
    assert sent == ["you@example.com"]
