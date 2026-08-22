"""Global administrator-managed Markdown usage document storage.

The document is intentionally separate from chat history and API storage.  It
is a single public document, persisted in the web users database with an
optimistic version lock so two administrator tabs cannot silently overwrite
one another.  The seed/sync source is the git-tracked
``config/usage_document.md``; a deployment can overwrite the server-side row
from that file without a service restart via ``sync_usage_document_from_source``.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "users.db"
# The seed/sync source ships with the repository so a local edit can reach the
# production row through git pull + scripts/sync_usage_document.py.
_SOURCE_PATH = _PROJECT_ROOT / "config" / "usage_document.md"
_MAX_CONTENT_CHARS = 500_000
_CACHE_TTL_SECONDS = 5.0

DEFAULT_CONTENT = _SOURCE_PATH.read_text(encoding="utf-8")


class UsageDocumentError(ValueError):
    """Expected validation or persistence error."""


class UsageDocumentVersionConflict(UsageDocumentError):
    """The document was changed after the editor loaded it."""


@dataclass(frozen=True)
class UsageDocument:
    title: str
    content: str
    version: int
    updated_by: str
    updated_at: float


_cache: tuple[str, float, UsageDocument] | None = None


def reset_cache() -> None:
    global _cache
    _cache = None


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS usage_document (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               title TEXT NOT NULL,
               content TEXT NOT NULL,
               version INTEGER NOT NULL,
               updated_by TEXT NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    conn.commit()
    return conn


def _row_to_document(row: tuple[object, ...]) -> UsageDocument:
    return UsageDocument(
        title=str(row[0] or "使用文档"),
        content=str(row[1] or ""),
        version=int(row[2]),
        updated_by=str(row[3] or "bootstrap"),
        updated_at=float(row[4] or 0.0),
    )


def _validate_content(content: str) -> str:
    if not isinstance(content, str):
        raise UsageDocumentError("文档内容必须是文本")
    if len(content) > _MAX_CONTENT_CHARS:
        raise UsageDocumentError(f"文档内容不能超过 {_MAX_CONTENT_CHARS:,} 个字符")
    return content


def get_usage_document() -> UsageDocument:
    global _cache
    now = time.time()
    if _cache and _cache[0] == str(_DB_PATH) and now - _cache[1] < _CACHE_TTL_SECONDS:
        return _cache[2]

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT title, content, version, updated_by, updated_at "
            "FROM usage_document WHERE id = 1"
        ).fetchone()
        if row is None:
            now = time.time()
            conn.execute(
                "INSERT INTO usage_document "
                "(id, title, content, version, updated_by, updated_at) "
                "VALUES (1, ?, ?, 1, 'bootstrap', ?)",
                ("使用文档", DEFAULT_CONTENT, now),
            )
            conn.commit()
            row = ("使用文档", DEFAULT_CONTENT, 1, "bootstrap", now)
    finally:
        conn.close()

    document = _row_to_document(row)
    _cache = (str(_DB_PATH), now, document)
    return document


def update_usage_document(content: str, *, expected_version: int, updated_by: str) -> UsageDocument:
    global _cache
    if expected_version < 1:
        raise UsageDocumentError("文档版本号无效")
    content = _validate_content(content)
    if not isinstance(updated_by, str) or not updated_by.strip():
        updated_by = "administrator"

    current = get_usage_document()
    now = time.time()
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE usage_document SET content = ?, version = version + 1, "
            "updated_by = ?, updated_at = ? WHERE id = 1 AND version = ?",
            (content, updated_by[:120], now, expected_version),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise UsageDocumentVersionConflict("使用文档已被其他管理员更新，请刷新后再保存")
        conn.commit()
        row = conn.execute(
            "SELECT title, content, version, updated_by, updated_at "
            "FROM usage_document WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    document = _row_to_document(row)
    _cache = (str(_DB_PATH), now, document)
    return document


def sync_usage_document_from_source() -> tuple[bool, UsageDocument]:
    """Overwrite the persisted document with the git-tracked source file.

    Idempotent: when the row already matches the file nothing is written and
    the version stays put.  Otherwise the file replaces the current content
    (discarding any server-side administrator hot edits) and bumps the
    version, so the running service picks it up within the read-cache TTL.
    """
    content = _SOURCE_PATH.read_text(encoding="utf-8")
    current = get_usage_document()
    if current.content == content:
        return False, current
    updated = update_usage_document(
        content,
        expected_version=current.version,
        updated_by="script:sync_usage_document",
    )
    return True, updated


def document_asdict(document: UsageDocument | None = None) -> dict:
    return asdict(document or get_usage_document())
