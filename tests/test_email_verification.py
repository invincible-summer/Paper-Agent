"""Registration email verification: send-code endpoint and register flows."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import core.auth_settings_store as ass  # noqa: E402
import core.user_store as us  # noqa: E402
from app.api.v1 import auth as auth_api  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ass, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(us, "_DB_PATH", tmp_path / "users.db")
    ass.reset_cache()
    sent: list[tuple[str, str]] = []

    def fake_send(to: str, code: str, **_kwargs):
        sent.append((to, code))

    monkeypatch.setattr("core.email_sender.send_verification_email", fake_send)
    return {"sent": sent}


def _set_requirement(requirement: str) -> None:
    current = ass.get_auth_settings()
    ass.update_auth_settings(
        {"auth_required": True, "registration_open": True, "email_requirement": requirement},
        expected_version=current.version, updated_by="test")


def _register(username: str, password: str, email: str = "", code: str = "") -> dict:
    return auth_api.register(auth_api.Credentials(
        username=username, password=password, display_name="",
        email=email, verification_code=code))


def _send_code(email: str) -> dict:
    return auth_api.send_email_code(auth_api.EmailCodeRequest(email=email))


def _verified_flag(db, username: str) -> int:
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT email_verified FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    assert row is not None
    return int(row[0])


def test_send_code_requires_verify_mode(env):
    _set_requirement("none")
    with pytest.raises(HTTPException) as exc:
        _send_code("you@example.com")
    assert exc.value.status_code == 400
    assert not env["sent"]


def test_send_code_delivers_and_cools_down(env):
    _set_requirement("verify")
    result = _send_code("You@Example.com")
    assert result["status"] == "sent"
    assert result["expires_in"] == ass.CODE_TTL_SECONDS
    assert env["sent"] == [("you@example.com", env["sent"][0][1])]
    assert len(env["sent"][0][1]) == 6 and env["sent"][0][1].isdigit()

    with pytest.raises(HTTPException) as exc:
        _send_code("you@example.com")
    assert exc.value.status_code == 429


def test_send_code_failure_leaves_no_stored_code(env, monkeypatch):
    _set_requirement("verify")
    from core.email_sender import EmailSendError

    def broken(_to, _code, **_kw):
        raise EmailSendError("SMTP 认证失败")

    monkeypatch.setattr("core.email_sender.send_verification_email", broken)
    with pytest.raises(HTTPException) as exc:
        _send_code("you@example.com")
    assert exc.value.status_code == 502
    assert ass.email_code_cooldown_remaining("you@example.com") == 0.0


def test_register_none_mode_keeps_legacy_behavior(env):
    _set_requirement("none")
    result = _register("plain-user", "password123")
    assert result["user"]["email"] == ""
    assert _verified_flag(us._DB_PATH, "plain-user") == 0
    # 可选填写邮箱：仅收集，不验证。
    result = _register("with-email", "password123", email="opt@example.com")
    assert result["user"]["email"] == "opt@example.com"
    assert _verified_flag(us._DB_PATH, "with-email") == 0
    with pytest.raises(HTTPException) as exc:
        _register("bad-email", "password123", email="not-an-email")
    assert exc.value.status_code == 400


def test_register_collect_mode_requires_email(env):
    _set_requirement("collect")
    with pytest.raises(HTTPException) as exc:
        _register("no-email", "password123")
    assert exc.value.status_code == 422
    result = _register("collector", "password123", email="Collector@Example.com")
    assert result["user"]["email"] == "collector@example.com"
    assert _verified_flag(us._DB_PATH, "collector") == 0


def test_register_verify_mode_full_flow(env):
    _set_requirement("verify")
    with pytest.raises(HTTPException) as missing_email:
        _register("verify-user", "password123", code="123456")
    assert missing_email.value.status_code == 422

    _send_code("Verify@Example.com")
    code = env["sent"][-1][1]
    with pytest.raises(HTTPException) as wrong:
        _register("verify-user", "password123",
                  email="verify@example.com", code="000000")
    assert wrong.value.status_code == 400

    result = _register("verify-user", "password123",
                       email="Verify@Example.com", code=code)
    assert result["user"]["email"] == "verify@example.com"
    assert result["user"]["role"] == "user"
    assert _verified_flag(us._DB_PATH, "verify-user") == 1

    # 验证码一次性消费；同邮箱不能再注册第二个账号。
    with pytest.raises(HTTPException) as consumed:
        _register("second-user", "password123",
                  email="verify@example.com", code=code)
    assert consumed.value.status_code == 400
    # 端点有 60s 重发冷却，这里直接在存储层放入新码来测邮箱唯一性。
    new_code = ass.generate_email_code()
    ass.record_email_code("verify@example.com", ass.hash_email_code(new_code))
    with pytest.raises(HTTPException) as duplicate:
        _register("third-user", "password123",
                  email="verify@example.com", code=new_code)
    assert duplicate.value.status_code == 400
    assert "邮箱已被绑定" in str(duplicate.value.detail)


def test_registration_closed_rejects_register_and_send_code(env):
    current = ass.get_auth_settings()
    ass.update_auth_settings(
        {"auth_required": True, "registration_open": False, "email_requirement": "verify"},
        expected_version=current.version, updated_by="test")
    with pytest.raises(HTTPException) as reg:
        _register("new-user", "password123", email="a@b.com", code="123456")
    assert reg.value.status_code == 403
    with pytest.raises(HTTPException) as code_call:
        _send_code("a@b.com")
    assert code_call.value.status_code == 403
    assert not env["sent"]


def test_bootstrap_administrator_allows_empty_email(env):
    admin, created = us.bootstrap_administrator(
        "administrator", "", "admin-password", "系统管理员")
    assert created is True
    assert admin["role"] == "administrator"
    assert admin["email"] == ""
    assert _verified_flag(us._DB_PATH, "administrator") == 1
