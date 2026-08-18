"""Multi-user accounts, browser tokens, and administrator Agent API keys.

All secret material is one-way protected with Python's standard library:
passwords use PBKDF2-HMAC-SHA256; browser and Agent API tokens are stored only
as SHA-256 hashes. SQLite runs in WAL mode and the schema migrates in place.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "users.db"

_ITERATIONS = 600_000
_DUMMY_PASSWORD_HASH = "pbkdf2$600000$00000000000000000000000000000000$" \
    "859683b26725e72dbd5a00678707949aca018faf730d7be6189da067dc075d24"
_TOKEN_TTL_SECONDS = 30 * 24 * 3600
_AGENT_KEY_TOUCH_SECONDS = 300
_USERNAME_RE = re.compile(r"^[\w一-鿿][\w一-鿿.-]{1,31}$", re.UNICODE)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")

LOCAL_USER = {
    "id": "local", "username": "local", "display_name": "本地用户",
    "email": "", "role": "local",
}


class UserStoreError(Exception):
    """Expected validation/conflict error safe to expose to a caller."""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
               id TEXT PRIMARY KEY,
               username TEXT NOT NULL UNIQUE COLLATE NOCASE,
               password_hash TEXT NOT NULL,
               display_name TEXT NOT NULL DEFAULT '',
               created_at REAL NOT NULL,
               email TEXT NOT NULL DEFAULT '',
               role TEXT NOT NULL DEFAULT 'user',
               disabled INTEGER NOT NULL DEFAULT 0
           )"""
    )
    columns = _columns(conn, "users")
    if "email" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    if "role" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    if "disabled" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
    if "email_verified" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
    # 注册邮箱绑定唯一性：空邮箱（未绑定）不受约束；存量重复时跳过索引而不是启动失败。
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email "
            "ON users(email) WHERE email <> ''")
    except sqlite3.DatabaseError:
        pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tokens (
               token_hash TEXT PRIMARY KEY,
               user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
               created_at REAL NOT NULL,
               expires_at REAL NOT NULL,
               last_used REAL NOT NULL
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_api_keys (
               id TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               key_hash TEXT NOT NULL UNIQUE,
               key_prefix TEXT NOT NULL,
               key_suffix TEXT NOT NULL,
               created_by TEXT NOT NULL REFERENCES users(id),
               created_at REAL NOT NULL,
               last_used_at REAL,
               revoked_at REAL
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_api_keys_active "
        "ON agent_api_keys(revoked_at, key_hash)"
    )
    conn.commit()
    return conn


def _hash_password(password: str, salt: bytes | None = None,
                   iterations: int = _ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iters, salt_hex, hash_hex = stored.split("$")
        if algorithm != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _user_dict(row) -> dict:
    return {
        "id": row[0], "username": row[1], "display_name": row[2],
        "email": row[3] or "", "role": row[4] or "user",
    }


def _validate_password(password: str) -> str:
    value = password or ""
    if len(value) < 8:
        raise UserStoreError("密码至少 8 位")
    if len(value) > 128:
        raise UserStoreError("密码过长（上限 128 位）")
    return value


def validate_credentials(username: str, password: str) -> tuple[str, str]:
    username = (username or "").strip()
    if not _USERNAME_RE.fullmatch(username):
        raise UserStoreError("用户名需 2-32 个字符，仅限中英文/数字/._-，且不能以符号开头")
    return username, _validate_password(password)


def _validate_email(email: str) -> str:
    value = (email or "").strip()[:254]
    if value and not _EMAIL_RE.match(value):
        raise UserStoreError("邮箱格式不正确")
    return value


def create_user(username: str, password: str, display_name: str = "",
                *, email: str = "", role: str = "user",
                email_verified: bool = False) -> dict:
    username, password = validate_credentials(username, password)
    email = _validate_email(email)
    if role not in {"user", "administrator"}:
        raise UserStoreError("无效的用户角色")
    uid = uuid.uuid4().hex
    shown_name = (display_name or username).strip()[:40]
    conn = _connect()
    try:
        if email:
            duplicate = conn.execute(
                "SELECT 1 FROM users WHERE email = ? AND email <> ''", (email,)
            ).fetchone()
            if duplicate is not None:
                raise UserStoreError("该邮箱已被绑定")
        conn.execute(
            "INSERT INTO users "
            "(id, username, password_hash, display_name, created_at, email, role, "
            " disabled, email_verified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (uid, username, _hash_password(password), shown_name,
             time.time(), email, role, int(bool(email_verified))),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # 用户名唯一索引与邮箱部分索引都会走到这里；按约束名区分提示。
        if "email" in str(exc).lower():
            raise UserStoreError("该邮箱已被绑定") from None
        raise UserStoreError("用户名已被占用") from None
    finally:
        conn.close()
    return {"id": uid, "username": username, "display_name": shown_name,
            "email": email, "role": role}


def bootstrap_administrator(username: str, email: str, password: str,
                            display_name: str = "系统管理员") -> tuple[dict, bool]:
    """Create the first administrator once; never resets an existing password.

    管理员账号对邮箱完全豁免：可留空，填了也不需要验证（email_verified=1）。
    """
    username, password = validate_credentials(username, password)
    email = _validate_email(email)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, username, display_name, email, role FROM users "
            "WHERE username = ? COLLATE NOCASE", (username,),
        ).fetchone()
        if row is not None:
            if row[4] != "administrator":
                raise UserStoreError("同名账号已存在但不是管理员，拒绝自动提权")
            return _user_dict(row), False
    finally:
        conn.close()
    return create_user(username, password, display_name,
                       email=email, role="administrator", email_verified=True), True


def authenticate(username: str, password: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, username, display_name, email, role, password_hash, disabled "
            "FROM users WHERE username = ? COLLATE NOCASE",
            ((username or "").strip(),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        # Keep missing-user timing close to a real password check without
        # paying for a second PBKDF2 hash generation on every failed login.
        _verify_password(password or "", _DUMMY_PASSWORD_HASH)
        return None
    if row[6] or not _verify_password(password or "", row[5]):
        return None
    return _user_dict(row[:5])


def change_password(user_id: str, current_password: str, new_password: str) -> None:
    new_password = _validate_password(new_password)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ? AND disabled = 0", (user_id,),
        ).fetchone()
        if row is None or not _verify_password(current_password or "", row[0]):
            raise UserStoreError("当前密码不正确")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (_hash_password(new_password), user_id))
        conn.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def issue_token(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn = _connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND disabled = 0", (user_id,),
        ).fetchone()
        if exists is None:
            raise UserStoreError("用户不存在或已停用")
        conn.execute(
            "INSERT INTO tokens (token_hash, user_id, created_at, expires_at, last_used) "
            "VALUES (?, ?, ?, ?, ?)",
            (_token_hash(token), user_id, now, now + _TOKEN_TTL_SECONDS, now),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def resolve_token(token: str) -> dict | None:
    if not token:
        return None
    now = time.time()
    digest = _token_hash(token)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT u.id, u.username, u.display_name, u.email, u.role, "
            "t.expires_at, u.disabled FROM tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token_hash = ?", (digest,),
        ).fetchone()
        if row is None or row[5] < now or row[6]:
            if row is not None:
                conn.execute("DELETE FROM tokens WHERE token_hash = ?", (digest,))
                conn.commit()
            return None
        conn.execute("UPDATE tokens SET last_used = ? WHERE token_hash = ?", (now, digest))
        conn.commit()
        return _user_dict(row[:5])
    finally:
        conn.close()


def revoke_token(token: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM tokens WHERE token_hash = ?", (_token_hash(token),))
        conn.commit()
    finally:
        conn.close()


def count_users() -> int:
    conn = _connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
    finally:
        conn.close()


def list_users() -> list[dict]:
    """List local database accounts (non-secret fields) for the admin console."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, username, display_name, email, role, disabled, created_at "
            "FROM users ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()
    out = [dict(LOCAL_USER) | {"disabled": 0, "created_at": 0}]
    out.extend({
        "id": row[0], "username": row[1], "display_name": row[2],
        "email": row[3] or "", "role": row[4] or "user",
        "disabled": int(row[5] or 0), "created_at": float(row[6] or 0),
    } for row in rows)
    return out


def create_agent_api_key(name: str, created_by: str) -> tuple[dict, str]:
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise UserStoreError("密钥名称需为 1-80 个字符")
    raw_key = "pa_live_" + secrets.token_urlsafe(36)
    record = {
        "id": uuid.uuid4().hex,
        "name": name,
        "key_prefix": raw_key[:15],
        "key_suffix": raw_key[-6:],
        "created_at": time.time(),
        "last_used_at": None,
        "revoked_at": None,
    }
    conn = _connect()
    try:
        owner = conn.execute(
            "SELECT role FROM users WHERE id = ? AND disabled = 0", (created_by,),
        ).fetchone()
        if owner is None or owner[0] != "administrator":
            raise UserStoreError("只有管理员可以创建 Agent API Key")
        conn.execute(
            "INSERT INTO agent_api_keys "
            "(id, name, key_hash, key_prefix, key_suffix, created_by, created_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (record["id"], name, _token_hash(raw_key), record["key_prefix"],
             record["key_suffix"], created_by, record["created_at"]),
        )
        conn.commit()
    finally:
        conn.close()
    return record, raw_key


def list_agent_api_keys() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, name, key_prefix, key_suffix, created_at, last_used_at, revoked_at "
            "FROM agent_api_keys ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": row[0], "name": row[1], "key_prefix": row[2],
         "key_suffix": row[3], "created_at": row[4], "last_used_at": row[5],
         "revoked_at": row[6]}
        for row in rows
    ]


def revoke_agent_api_key(key_id: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE agent_api_keys SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL", (time.time(), key_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_agent_api_keys() -> int:
    """Count all keys, including revoked records, without exposing secrets."""
    conn = _connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM agent_api_keys").fetchone()[0])
    finally:
        conn.close()


def count_active_agent_api_keys() -> int:
    conn = _connect()
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM agent_api_keys WHERE revoked_at IS NULL"
        ).fetchone()[0])
    finally:
        conn.close()


def resolve_agent_api_key(token: str) -> dict | None:
    """Resolve an Agent key to a non-secret principal record.

    The raw token and its database hash are never returned.  Callers receive
    only stable identifiers suitable for API-session partitioning.
    """
    if not token:
        return None
    digest = _token_hash(token)
    now = time.time()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, created_by, last_used_at FROM agent_api_keys "
            "WHERE key_hash = ? AND revoked_at IS NULL", (digest,),
        ).fetchone()
        if row is None:
            return None
        last_used = row[2]
        if last_used is None or now - float(last_used) >= _AGENT_KEY_TOUCH_SECONDS:
            conn.execute(
                "UPDATE agent_api_keys SET last_used_at = ? WHERE id = ?",
                (now, row[0]),
            )
            conn.commit()
        return {"key_id": row[0], "created_by": row[1], "source": "database"}
    finally:
        conn.close()


def verify_agent_api_key(token: str) -> bool:
    return resolve_agent_api_key(token) is not None
