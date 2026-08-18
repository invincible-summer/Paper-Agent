"""Administrator bootstrap, password lifecycle, and long-lived Agent API keys."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import core.auth_settings_store as ass
import core.user_store as us
from app.core.config import settings
from app.main import create_app


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(ass, "_DB_PATH", tmp_path / "users.db")
    ass.reset_cache()
    return us


def test_legacy_users_schema_migrates_without_data_loss(isolated_store):
    db_path = isolated_store._DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE "
        "COLLATE NOCASE, password_hash TEXT NOT NULL, display_name TEXT NOT NULL "
        "DEFAULT '', created_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
        ("legacy-id", "legacy", isolated_store._hash_password("password123"), "旧用户", 1.0),
    )
    conn.commit()
    conn.close()

    user = isolated_store.authenticate("legacy", "password123")
    assert user == {
        "id": "legacy-id",
        "username": "legacy",
        "display_name": "旧用户",
        "email": "",
        "role": "user",
    }
    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    conn.close()
    assert {"email", "role", "disabled"} <= columns


def test_bootstrap_administrator_is_idempotent_and_never_resets_password(isolated_store):
    admin, created = isolated_store.bootstrap_administrator(
        "administrator", "administrator@administrator", "first-password"
    )
    assert created is True
    assert admin["role"] == "administrator"

    same, created_again = isolated_store.bootstrap_administrator(
        "ADMINISTRATOR", "administrator@administrator", "different-password"
    )
    assert created_again is False
    assert same["id"] == admin["id"]
    assert isolated_store.authenticate("administrator", "first-password") is not None
    assert isolated_store.authenticate("administrator", "different-password") is None


def test_bootstrap_refuses_to_promote_existing_user(isolated_store):
    isolated_store.create_user("administrator", "ordinary-password")
    with pytest.raises(isolated_store.UserStoreError, match="拒绝自动提权"):
        isolated_store.bootstrap_administrator(
            "administrator", "administrator@administrator", "admin-password"
        )


def test_change_password_revokes_all_browser_tokens(isolated_store):
    user = isolated_store.create_user("alice", "old-password")
    token_a = isolated_store.issue_token(user["id"])
    token_b = isolated_store.issue_token(user["id"])

    isolated_store.change_password(user["id"], "old-password", "new-password")

    assert isolated_store.authenticate("alice", "old-password") is None
    assert isolated_store.authenticate("alice", "new-password") is not None
    assert isolated_store.resolve_token(token_a) is None
    assert isolated_store.resolve_token(token_b) is None


def test_agent_key_is_plaintext_once_and_hash_only_at_rest(isolated_store):
    admin, _ = isolated_store.bootstrap_administrator(
        "administrator", "administrator@administrator", "admin-password"
    )
    item, raw_key = isolated_store.create_agent_api_key("清小搭生产接入", admin["id"])
    assert raw_key.startswith("pa_live_")
    assert item["key_suffix"] == raw_key[-6:]
    assert isolated_store.verify_agent_api_key(raw_key) is True
    principal = isolated_store.resolve_agent_api_key(raw_key)
    assert principal == {
        "key_id": item["id"], "created_by": admin["id"], "source": "database"
    }
    assert raw_key not in repr(principal)

    conn = sqlite3.connect(isolated_store._DB_PATH)
    stored = conn.execute(
        "SELECT key_hash, key_prefix, key_suffix FROM agent_api_keys WHERE id = ?",
        (item["id"],),
    ).fetchone()
    conn.close()
    assert stored[0] == hashlib.sha256(raw_key.encode()).hexdigest()
    assert raw_key not in stored
    assert raw_key not in repr(isolated_store.list_agent_api_keys())

    assert isolated_store.revoke_agent_api_key(item["id"]) is True
    assert isolated_store.verify_agent_api_key(raw_key) is False
    assert isolated_store.revoke_agent_api_key(item["id"]) is False


def test_admin_routes_and_database_key_auth(isolated_store, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "guest_access", False)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    admin, _ = isolated_store.bootstrap_administrator(
        "administrator", "administrator@administrator", "admin-password"
    )
    normal = isolated_store.create_user("normal-user", "normal-password")
    admin_token = isolated_store.issue_token(admin["id"])
    normal_token = isolated_store.issue_token(normal["id"])

    from fastapi import HTTPException
    from app.api.v1 import admin as admin_api
    from app.api.v1.openai_compat import _check_auth

    with pytest.raises(HTTPException) as forbidden:
        admin_api.get_agent_keys(f"Bearer {normal_token}")
    assert forbidden.value.status_code == 403

    created = admin_api.post_agent_key(
        admin_api.AgentKeyCreateRequest(name="清小搭"), f"Bearer {admin_token}"
    )
    raw_key = created["key"]
    key_id = created["item"]["id"]

    listed = admin_api.get_agent_keys(f"Bearer {admin_token}")
    assert raw_key not in repr(listed)
    assert listed["items"][0]["id"] == key_id

    api_principal = _check_auth(f"Bearer {raw_key}")
    assert api_principal.key_id == key_id
    assert api_principal.created_by == admin["id"]
    assert api_principal.source == "database"
    assert raw_key not in repr(api_principal)
    revoked = admin_api.delete_agent_key(key_id, f"Bearer {admin_token}")
    assert revoked == {"status": "revoked", "id": key_id}
    with pytest.raises(HTTPException) as denied:
        _check_auth(f"Bearer {raw_key}")
    assert denied.value.status_code == 401


def test_guest_disable_and_password_change_endpoint(isolated_store, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "guest_access", False)
    user = isolated_store.create_user("account-user", "old-password")
    token = isolated_store.issue_token(user["id"])
    from fastapi import HTTPException
    from app.api.v1 import auth as auth_api

    with pytest.raises(HTTPException) as guest:
        auth_api.me(authorization=None, x_guest_id="browser-abc-123")
    assert guest.value.status_code == 401

    changed = auth_api.password_change(
        auth_api.PasswordChangeRequest(
            current_password="old-password", new_password="new-password"
        ),
        f"Bearer {token}",
    )
    assert changed["reauthenticate"] is True
    with pytest.raises(HTTPException) as expired:
        auth_api.me(authorization=f"Bearer {token}")
    assert expired.value.status_code == 401
