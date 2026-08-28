"""用户反馈存储：所有登录用户 / 游客 / 本地模式均可提交，仅管理员可查看。

反馈是追加型记录数据（而非版本化设置行），沿用 user_store 的模块级函数 +
原生 sqlite3 + 幂等建表模式，数据库为 data/users.db（本地运行时数据，不进 Git）。
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "users.db"

CATEGORIES = ("问题报告", "功能建议", "其他")
_COOLDOWN_SECONDS = 60
_CONTENT_MAX_CHARS = 4000
_CONTACT_MAX_CHARS = 120
_LIST_LIMIT_MAX = 200


class FeedbackError(Exception):
    """Expected validation/conflict error safe to expose to a caller."""


class FeedbackCooldownError(FeedbackError):
    """Same submitter is within the anti-spam cooldown window."""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS feedback (
               id TEXT PRIMARY KEY,
               user_id TEXT NOT NULL,
               username TEXT NOT NULL,
               role TEXT NOT NULL,
               category TEXT NOT NULL,
               content TEXT NOT NULL,
               contact TEXT NOT NULL DEFAULT '',
               status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
               created_at REAL NOT NULL,
               resolved_at REAL,
               resolved_by TEXT
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at)")
    conn.commit()
    return conn


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "user_id": str(row["user_id"]),
        "username": str(row["username"]),
        "role": str(row["role"]),
        "category": str(row["category"]),
        "content": str(row["content"]),
        "contact": str(row["contact"] or ""),
        "status": str(row["status"]),
        "created_at": float(row["created_at"]),
        "resolved_at": float(row["resolved_at"]) if row["resolved_at"] is not None else None,
        "resolved_by": str(row["resolved_by"]) if row["resolved_by"] is not None else None,
    }


def create_feedback(user: dict, category: str, content: str,
                    contact: str = "") -> dict[str, Any]:
    """记录一条反馈；同一提交者 60 秒内重复提交会被拒绝（防刷）。"""
    category = (category or "其他").strip() or "其他"
    if category not in CATEGORIES:
        raise FeedbackError(f"反馈类型必须是 {' / '.join(CATEGORIES)} 之一")
    content = (content or "").strip()
    if not content:
        raise FeedbackError("反馈内容不能为空")
    if len(content) > _CONTENT_MAX_CHARS:
        raise FeedbackError(f"反馈内容不能超过 {_CONTENT_MAX_CHARS} 字")
    contact = (contact or "").strip()
    if len(contact) > _CONTACT_MAX_CHARS:
        raise FeedbackError(f"联系方式不能超过 {_CONTACT_MAX_CHARS} 字")
    user_id = str(user.get("id") or "anonymous")
    username = str(user.get("username") or user.get("display_name") or "匿名")
    role = str(user.get("role") or "user")
    now = time.time()
    item_id = uuid.uuid4().hex
    with _connect() as conn:
        recent = conn.execute(
            "SELECT created_at FROM feedback WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 1", (user_id,)).fetchone()
        if recent is not None and now - float(recent["created_at"]) < _COOLDOWN_SECONDS:
            remaining = int(_COOLDOWN_SECONDS - (now - float(recent["created_at"]))) + 1
            raise FeedbackCooldownError(f"提交过于频繁，请 {remaining} 秒后再试")
        conn.execute(
            "INSERT INTO feedback(id, user_id, username, role, category, content, "
            "contact, status, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (item_id, user_id, username, role, category, content, contact, now),
        )
        conn.commit()
    return {
        "id": item_id, "user_id": user_id, "username": username, "role": role,
        "category": category, "content": content, "contact": contact,
        "status": "open", "created_at": now, "resolved_at": None, "resolved_by": None,
    }


def list_feedback(status: str | None = None, offset: int = 0,
                  limit: int = 50) -> dict[str, Any]:
    """按时间倒序列出反馈；status 为 'open'/'resolved' 时只取对应状态。"""
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), _LIST_LIMIT_MAX))
    where = ""
    params: list[Any] = []
    if status in {"open", "resolved"}:
        where = "WHERE status = ?"
        params.append(status)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM feedback {where} "
            f"ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
        counts_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM feedback GROUP BY status").fetchall()
        counts = {status_: 0 for status_ in ("open", "resolved")}
        for row in counts_rows:
            counts[str(row["status"])] = int(row["n"])
    return {
        "items": [_row_to_item(row) for row in rows],
        "counts": {"open": counts["open"], "resolved": counts["resolved"],
                   "total": counts["open"] + counts["resolved"]},
    }


def set_feedback_status(feedback_id: str, resolved: bool,
                        resolved_by: str) -> dict[str, Any] | None:
    """标记已处理 / 重新打开；返回更新后的条目，不存在时返回 None。"""
    now = time.time()
    with _connect() as conn:
        if resolved:
            cursor = conn.execute(
                "UPDATE feedback SET status = 'resolved', resolved_at = ?, "
                "resolved_by = ? WHERE id = ?",
                (now, resolved_by, feedback_id))
        else:
            cursor = conn.execute(
                "UPDATE feedback SET status = 'open', resolved_at = NULL, "
                "resolved_by = NULL WHERE id = ?", (feedback_id,))
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    return _row_to_item(row) if row is not None else None


def delete_feedback(feedback_id: str) -> bool:
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM feedback WHERE id = ?", (feedback_id,))
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
    return True
