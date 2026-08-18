"""Runtime web-authentication settings, persisted in data/users.db.

The .env flags (AUTH_REQUIRED / REGISTRATION_OPEN / GUEST_ACCESS /
EMAIL_REQUIREMENT) only seed the single ``web_auth_settings`` row the first
time it is read; afterwards the database row is the sole source of truth so
administrator changes survive restarts.  Updates take an optimistic version
lock exactly like ``core/api_storage_store.py``.

Also owns the ``email_codes`` table (registration verification codes: hashed,
expiring, attempt-limited).  Single Uvicorn worker only by design: reads are
served from a short-TTL in-process cache that is refreshed immediately on
in-process writes, so out-of-process maintenance (e.g. recovery scripts)
propagates within a few seconds.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "users.db"

_EMAIL_REQUIREMENTS = ("none", "collect", "verify")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")

# 验证码参数：10 分钟有效、60 秒重发冷却、最多 5 次尝试。
CODE_TTL_SECONDS = 600
CODE_COOLDOWN_SECONDS = 60
CODE_MAX_ATTEMPTS = 5

# 读缓存 TTL：进程内写入立即刷新，跨进程修改最多 5 秒后可见。
_CACHE_TTL_SECONDS = 5.0


class AuthSettingsError(Exception):
    """Expected validation/conflict error safe to expose to a caller."""


class AuthSettingsVersionConflict(AuthSettingsError):
    """Optimistic update lost a race (another administrator wrote first)."""


class EmailCodeError(AuthSettingsError):
    """Verification-code validation failure with a user-facing message."""


@dataclass(frozen=True)
class AuthSettings:
    auth_required: bool = False
    guest_access: bool = True
    registration_open: bool = True
    email_requirement: str = "none"
    version: int = 1
    updated_by: str = "bootstrap"
    updated_at: float = 0.0


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS web_auth_settings (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               auth_required INTEGER NOT NULL,
               guest_access INTEGER NOT NULL,
               registration_open INTEGER NOT NULL,
               email_requirement TEXT NOT NULL,
               version INTEGER NOT NULL,
               updated_by TEXT NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS email_codes (
               email TEXT PRIMARY KEY,
               code_hash TEXT NOT NULL,
               expires_at REAL NOT NULL,
               attempts INTEGER NOT NULL DEFAULT 0,
               last_sent_at REAL NOT NULL
           )"""
    )
    conn.commit()
    return conn


def _env_seed() -> dict:
    """Initial row values from the backend .env (best-effort; core must stay
    importable outside the backend process, e.g. for recovery scripts)."""
    try:
        from app.core.config import settings  # noqa: PLC0415 — deliberate bridge
        return {
            "auth_required": bool(settings.auth_required),
            "guest_access": bool(settings.guest_access),
            "registration_open": bool(settings.registration_open),
            "email_requirement": str(settings.email_requirement),
        }
    except Exception:  # noqa: BLE001 — standalone/scripts fallback
        return {"auth_required": False, "guest_access": True,
                "registration_open": True, "email_requirement": "none"}


_cache: tuple[str, float, AuthSettings] | None = None


def reset_cache() -> None:
    """Drop the in-process read cache (tests / forced refresh)."""
    global _cache
    _cache = None


def _row_to_settings(row) -> AuthSettings:
    requirement = str(row[3]) if row[3] in _EMAIL_REQUIREMENTS else "none"
    return AuthSettings(
        auth_required=bool(row[0]),
        guest_access=bool(row[1]),
        registration_open=bool(row[2]),
        email_requirement=requirement,
        version=int(row[4]),
        updated_by=str(row[5] or "bootstrap"),
        updated_at=float(row[6] or 0.0),
    )


def get_auth_settings() -> AuthSettings:
    """Current settings, seeding the row from .env on first use."""
    global _cache
    now = time.time()
    if _cache is not None and _cache[0] == str(_DB_PATH) and now - _cache[1] < _CACHE_TTL_SECONDS:
        return _cache[2]
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT auth_required, guest_access, registration_open, "
            "email_requirement, version, updated_by, updated_at "
            "FROM web_auth_settings WHERE id = 1"
        ).fetchone()
        if row is None:
            seed = _env_seed()
            if seed["email_requirement"] not in _EMAIL_REQUIREMENTS:
                seed["email_requirement"] = "none"
            conn.execute(
                "INSERT INTO web_auth_settings "
                "(id, auth_required, guest_access, registration_open, "
                " email_requirement, version, updated_by, updated_at) "
                "VALUES (1, ?, ?, ?, ?, 1, 'bootstrap', ?)",
                (int(seed["auth_required"]), int(seed["guest_access"]),
                 int(seed["registration_open"]), seed["email_requirement"], now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT auth_required, guest_access, registration_open, "
                "email_requirement, version, updated_by, updated_at "
                "FROM web_auth_settings WHERE id = 1"
            ).fetchone()
    finally:
        conn.close()
    settings_row = _row_to_settings(row)
    _cache = (str(_DB_PATH), now, settings_row)
    return settings_row


def update_auth_settings(changes: dict, *, expected_version: int,
                         updated_by: str) -> AuthSettings:
    """Optimistic-lock update of the four runtime flags."""
    allowed = {"auth_required", "guest_access", "registration_open", "email_requirement"}
    unknown = set(changes) - allowed
    if unknown:
        raise AuthSettingsError(f"不支持的设置字段：{', '.join(sorted(unknown))}")
    if "email_requirement" in changes and changes["email_requirement"] not in _EMAIL_REQUIREMENTS:
        raise AuthSettingsError("email_requirement 仅支持 none / collect / verify")
    actor = (updated_by or "").strip()
    if not actor or len(actor) > 128:
        raise AuthSettingsError("updated_by 需为 1-128 个字符")
    if not changes:
        return get_auth_settings()
    assignments = ", ".join(f"{key} = ?" for key in sorted(changes))
    params: list = []
    for key in sorted(changes):
        value = changes[key]
        params.append(int(value) if isinstance(value, bool) else str(value))
    params.extend([actor, time.time(), int(expected_version)])
    conn = _connect()
    try:
        cursor = conn.execute(
            f"UPDATE web_auth_settings SET {assignments}, "
            "updated_by = ?, updated_at = ?, version = version + 1 "
            "WHERE id = 1 AND version = ?",
            params,
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise AuthSettingsVersionConflict(
                f"auth settings version conflict (expected {expected_version})")
        conn.commit()
    finally:
        conn.close()
    reset_cache()
    return get_auth_settings()


# ---------------------------------------------------------------------------
# Registration email verification codes
# ---------------------------------------------------------------------------

def normalize_email(email: str) -> str:
    value = (email or "").strip().lower()[:254]
    if not value or not _EMAIL_RE.match(value):
        raise AuthSettingsError("邮箱格式不正确")
    return value


def hash_email_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def email_code_cooldown_remaining(email: str) -> float:
    """Seconds until a new code may be sent (0 = allowed now)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT last_sent_at FROM email_codes WHERE email = ?", (email,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0.0
    remaining = CODE_COOLDOWN_SECONDS - (time.time() - float(row[0]))
    return max(0.0, remaining)


def record_email_code(email: str, code_hash: str) -> None:
    """Persist a freshly delivered code (replaces any previous one)."""
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO email_codes (email, code_hash, expires_at, attempts, last_sent_at) "
            "VALUES (?, ?, ?, 0, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "code_hash = excluded.code_hash, expires_at = excluded.expires_at, "
            "attempts = 0, last_sent_at = excluded.last_sent_at",
            (email, code_hash, now + CODE_TTL_SECONDS, now),
        )
        conn.commit()
    finally:
        conn.close()


def verify_email_code(email: str, code: str) -> None:
    """Consume a valid code; raise EmailCodeError otherwise."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT code_hash, expires_at, attempts FROM email_codes WHERE email = ?",
            (email,),
        ).fetchone()
        if row is None:
            raise EmailCodeError("请先获取邮箱验证码")
        code_hash, expires_at, attempts = str(row[0]), float(row[1]), int(row[2])
        if time.time() > expires_at:
            conn.execute("DELETE FROM email_codes WHERE email = ?", (email,))
            conn.commit()
            raise EmailCodeError("验证码已过期，请重新获取")
        if attempts >= CODE_MAX_ATTEMPTS:
            conn.execute("DELETE FROM email_codes WHERE email = ?", (email,))
            conn.commit()
            raise EmailCodeError("错误次数过多，请重新获取验证码")
        if not hmac.compare_digest(hash_email_code(code or ""), code_hash):
            if attempts + 1 >= CODE_MAX_ATTEMPTS:
                conn.execute("DELETE FROM email_codes WHERE email = ?", (email,))
            else:
                conn.execute(
                    "UPDATE email_codes SET attempts = attempts + 1 WHERE email = ?",
                    (email,),
                )
            conn.commit()
            raise EmailCodeError("验证码错误")
        conn.execute("DELETE FROM email_codes WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()


def generate_email_code() -> str:
    """6-digit numeric code (cryptographically random)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def settings_asdict() -> dict:
    return asdict(get_auth_settings())
