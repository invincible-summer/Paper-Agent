"""Guest-mode identity resolution (AUTH_REQUIRED on, login optional)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import settings  # noqa: E402
from app.api.v1.auth import current_user  # noqa: E402


@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    return settings


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


def test_local_mode_ignores_everything(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    assert current_user(None, None)["id"] == "local"
    assert current_user("Bearer garbage", None)["id"] == "local"
