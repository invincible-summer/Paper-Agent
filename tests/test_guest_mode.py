"""Guest-mode identity resolution (AUTH_REQUIRED on, login optional)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import core.auth_settings_store as ass  # noqa: E402
import core.user_store as us  # noqa: E402
from app.api.v1.auth import current_user  # noqa: E402


def _set_flags(tmp_path, monkeypatch, **flags):
    """Isolated runtime settings row; the dev .env must not leak in."""
    monkeypatch.setattr(ass, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(us, "_DB_PATH", tmp_path / "users.db")
    ass.reset_cache()
    current = ass.get_auth_settings()
    merged = {
        "auth_required": current.auth_required,
        "guest_access": current.guest_access,
        "registration_open": current.registration_open,
        "email_requirement": current.email_requirement,
    }
    merged.update(flags)
    ass.update_auth_settings(merged, expected_version=current.version, updated_by="test")


@pytest.fixture()
def auth_on(tmp_path, monkeypatch):
    _set_flags(tmp_path, monkeypatch, auth_required=True, guest_access=True)


def test_guest_id_grants_isolated_identity(auth_on):
    u1 = current_user(None, "browser-abc-123")
    u2 = current_user(None, "browser-xyz-999")
    assert u1["id"] == "guest:browser-abc-123"
    assert u1["id"] != u2["id"]           # two guests never share data
    assert u1["id"] != "local"            # guests are not the local account


def test_no_identity_rejected(auth_on):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        current_user(None, None)
    assert e.value.status_code == 401


def test_bad_guest_id_rejected(auth_on):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        current_user(None, "../../etc/passwd")
    with pytest.raises(HTTPException):
        current_user(None, "short")


def test_invalid_token_rejected_even_with_guest(auth_on):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        current_user("Bearer not-a-real-token", "browser-abc-123")


def test_guest_off_rejects_guest_ids(tmp_path, monkeypatch):
    from fastapi import HTTPException
    _set_flags(tmp_path, monkeypatch, auth_required=True, guest_access=False)
    with pytest.raises(HTTPException) as e:
        current_user(None, "browser-abc-123")
    assert e.value.status_code == 401


def test_local_mode_ignores_everything(tmp_path, monkeypatch):
    _set_flags(tmp_path, monkeypatch, auth_required=False)
    assert current_user(None, None)["id"] == "local"
    assert current_user("Bearer garbage", None)["id"] == "local"
