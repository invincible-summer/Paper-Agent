"""Runtime auth settings store: env seeding, optimistic updates, email codes."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import core.auth_settings_store as ass  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ass, "_DB_PATH", tmp_path / "users.db")
    ass.reset_cache()
    return ass


def test_seeds_single_row_on_first_read(store):
    flags = store.get_auth_settings()
    assert flags.version == 1
    assert flags.email_requirement in ("none", "collect", "verify")
    assert isinstance(flags.auth_required, bool)
    # 单行表：再次读取不重复插入，也不依赖缓存 TTL。
    store.reset_cache()
    again = store.get_auth_settings()
    assert again == flags


def test_update_bumps_version_and_refreshes_cache(store):
    first = store.get_auth_settings()
    updated = store.update_auth_settings(
        {"guest_access": False, "email_requirement": "collect"},
        expected_version=first.version, updated_by="admin-1")
    assert updated.version == first.version + 1
    assert updated.guest_access is False
    assert updated.email_requirement == "collect"
    assert store.get_auth_settings() == updated  # 进程内写入立即刷新缓存


def test_update_version_conflict_raises(store):
    first = store.get_auth_settings()
    with pytest.raises(ass.AuthSettingsVersionConflict):
        store.update_auth_settings(
            {"guest_access": False},
            expected_version=first.version + 5, updated_by="admin-1")


def test_update_rejects_unknown_fields_and_bad_requirement(store):
    first = store.get_auth_settings()
    with pytest.raises(ass.AuthSettingsError):
        store.update_auth_settings(
            {"nope": 1}, expected_version=first.version, updated_by="admin-1")
    with pytest.raises(ass.AuthSettingsError):
        store.update_auth_settings(
            {"email_requirement": "sometimes"},
            expected_version=first.version, updated_by="admin-1")


def test_normalize_email(store):
    assert store.normalize_email("  User@Example.COM ") == "user@example.com"
    with pytest.raises(ass.AuthSettingsError):
        store.normalize_email("not-an-email")
    with pytest.raises(ass.AuthSettingsError):
        store.normalize_email("")


def test_email_code_lifecycle(store):
    email = store.normalize_email("User@Example.com")
    assert store.email_code_cooldown_remaining(email) == 0.0
    store.record_email_code(email, store.hash_email_code("123456"))
    assert 0 < store.email_code_cooldown_remaining(email) <= store.CODE_COOLDOWN_SECONDS

    with pytest.raises(ass.EmailCodeError, match="验证码错误"):
        store.verify_email_code(email, "000000")
    # 尝试上限内正确码仍有效，且一次性消费。
    store.verify_email_code(email, "123456")
    with pytest.raises(ass.EmailCodeError, match="请先获取"):
        store.verify_email_code(email, "123456")


def test_email_code_expires(store):
    email = "a@b.com"
    store.record_email_code(email, store.hash_email_code("123456"))
    conn = sqlite3.connect(store._DB_PATH)
    conn.execute("UPDATE email_codes SET expires_at = 1 WHERE email = ?", (email,))
    conn.commit()
    conn.close()
    with pytest.raises(ass.EmailCodeError, match="已过期"):
        store.verify_email_code(email, "123456")


def test_email_code_max_attempts_invalidates(store):
    email = "a@b.com"
    store.record_email_code(email, store.hash_email_code("123456"))
    # 第 MAX 次错误在报「验证码错误」的同时删除验证码。
    for _ in range(store.CODE_MAX_ATTEMPTS):
        with pytest.raises(ass.EmailCodeError, match="验证码错误"):
            store.verify_email_code(email, "000000")
    with pytest.raises(ass.EmailCodeError, match="请先获取"):
        store.verify_email_code(email, "123456")


def test_record_email_code_replaces_previous(store):
    email = "a@b.com"
    store.record_email_code(email, store.hash_email_code("111111"))
    store.record_email_code(email, store.hash_email_code("222222"))
    store.verify_email_code(email, "222222")
    with pytest.raises(ass.EmailCodeError, match="请先获取"):
        store.verify_email_code(email, "111111")
