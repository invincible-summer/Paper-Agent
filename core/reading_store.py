"""Web 阅读记录；所有操作显式携带 owner/session/document，不进入 API checkpoint。"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

READING_DB = Path(__file__).resolve().parents[1] / "data" / "reading.db"


class ReadingConflict(Exception):
    pass


@contextmanager
def connection(path: Path | None = None):
    path = path or READING_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA secure_delete=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS reading_sessions (
            owner TEXT NOT NULL, session TEXT NOT NULL, filename TEXT NOT NULL,
            PRIMARY KEY(owner, session), UNIQUE(owner, filename)
        );
        CREATE TABLE IF NOT EXISTS reading_records (
            owner TEXT NOT NULL, session TEXT NOT NULL, document TEXT NOT NULL,
            kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL,
            PRIMARY KEY(owner, session, document, kind, id),
            FOREIGN KEY(owner,session) REFERENCES reading_sessions(owner,session) ON DELETE CASCADE
        );
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def bind(owner: str, session: str, filename: str) -> None:
    with connection() as db:
        db.execute("INSERT INTO reading_sessions VALUES (?,?,?) "
                   "ON CONFLICT(owner,session) DO UPDATE SET filename=excluded.filename",
                   (owner, session, filename))


def filename_for(owner: str, session: str) -> str | None:
    with connection() as db:
        row = db.execute("SELECT filename FROM reading_sessions WHERE owner=? AND session=?",
                         (owner, session)).fetchone()
        return row[0] if row else None


def _decode(row) -> dict:
    return {**json.loads(row["payload"]), "id": row["id"], "version": row["version"],
            "updated": row["updated"]}


def get(owner: str, session: str, document: str, kind: str, record_id: str) -> dict | None:
    with connection() as db:
        row = db.execute("SELECT * FROM reading_records WHERE owner=? AND session=? AND document=? AND kind=? AND id=?",
                         (owner, session, document, kind, record_id)).fetchone()
        return _decode(row) if row else None


def list_records(owner: str, session: str, document: str, kind: str) -> list[dict]:
    with connection() as db:
        rows = db.execute("SELECT * FROM reading_records WHERE owner=? AND session=? AND document=? AND kind=? ORDER BY updated",
                          (owner, session, document, kind)).fetchall()
        return [_decode(r) for r in rows]


def _write(db, owner: str, session: str, document: str, kind: str, payload: dict,
           record_id: str | None = None, expected_version: int = 0) -> dict:
    record_id = record_id or uuid.uuid4().hex
    now = time.time()
    encoded = json.dumps(payload, ensure_ascii=False)
    scope = (owner, session, document, kind, record_id)
    if expected_version == 0:
        try:
            db.execute("INSERT INTO reading_records VALUES (?,?,?,?,?,?,1,?)", (*scope, encoded, now))
        except sqlite3.IntegrityError as exc:
            raise ReadingConflict("记录已存在或所属会话已删除，请刷新后重试") from exc
    else:
        changed = db.execute("UPDATE reading_records SET payload=?, version=version+1, updated=? "
                             "WHERE owner=? AND session=? AND document=? AND kind=? AND id=? AND version=?",
                             (encoded, now, *scope, expected_version))
        if changed.rowcount != 1:
            raise ReadingConflict("记录已在其他窗口更新，请刷新后重试；当前草稿未覆盖")
    return {**payload, "id": record_id, "version": expected_version + 1, "updated": now}


def put(owner: str, session: str, document: str, kind: str, payload: dict,
        record_id: str | None = None, expected_version: int = 0) -> dict:
    with connection() as db:
        return _write(db, owner, session, document, kind, payload, record_id, expected_version)


def finish_action(owner: str, session: str, document: str, request_id: str, result: dict,
                  thread_payload: dict | None = None, thread_id: str | None = None,
                  thread_version: int = 0) -> dict:
    """讨论与幂等结果同事务提交，重试缓存仅保存 thread 引用，避免复制整段历史。"""
    result = dict(result)
    with connection() as db:
        if thread_payload is not None:
            thread = _write(db, owner, session, document, "thread", thread_payload, thread_id, thread_version)
            result["thread_id"] = thread["id"]
        _write(db, owner, session, document, "action", result, request_id)
    if thread_payload is not None:
        result["thread"] = thread
    return result


def delete(owner: str, session: str, document: str, kind: str, record_id: str, version: int) -> None:
    with connection() as db:
        result = db.execute("DELETE FROM reading_records WHERE owner=? AND session=? AND document=? AND kind=? AND id=? AND version=?",
                            (owner, session, document, kind, record_id, version))
        if result.rowcount != 1:
            raise ReadingConflict("记录已变更，请刷新后重试")


def delete_session(owner: str, session: str, path: Path | None = None) -> None:
    if not (path or READING_DB).exists():
        return
    with connection(path) as db:
        db.execute("DELETE FROM reading_sessions WHERE owner=? AND session=?", (owner, session))


def delete_owner(owner: str, path: Path | None = None) -> None:
    if not (path or READING_DB).exists():
        return
    with connection(path) as db:
        db.execute("DELETE FROM reading_sessions WHERE owner=?", (owner,))
    with sqlite3.connect(path or READING_DB) as db:
        db.execute("VACUUM")


def owner_bytes(owner: str, path: Path | None = None) -> int:
    if not (path or READING_DB).exists():
        return 0
    with connection(path) as db:
        return db.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM reading_records WHERE owner=?", (owner,)).fetchone()[0]
