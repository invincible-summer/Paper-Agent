"""Ownership index for web-channel uploads and generated exports.

The files themselves remain under the legacy ``data/uploads`` and
``data/exports`` directories.  This small SQLite index binds each opaque file
identifier to one authenticated web identity so knowing another user's UUID or
export filename is not sufficient to read it.

OpenAI-compatible API artifacts deliberately do not use this store: their
private uploads are session-HMAC scoped under ``data/openai_api`` and their
short-lived public export aliases must be fetchable by the external platform.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ARTIFACT_DB = _PROJECT_ROOT / "data" / "web_artifacts.db"
_ALLOWED_KINDS = {"attachment", "export"}


def _connect() -> sqlite3.Connection:
    WEB_ARTIFACT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(WEB_ARTIFACT_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_artifact_owners (
            kind TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            PRIMARY KEY (kind, artifact_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_artifact_owner "
        "ON web_artifact_owners(owner_id, kind)"
    )
    return conn


def register_web_artifact(
    kind: str,
    artifact_id: str,
    owner_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Bind one web artifact to its owner without exposing owner data publicly.

    Re-registering the same id for a different owner is rejected.  File ids and
    export names are collision-resistant, so a conflict indicates tampering or
    a programming error rather than an ownership transfer.
    """
    if kind not in _ALLOWED_KINDS:
        raise ValueError("unsupported web artifact kind")
    artifact_id = (artifact_id or "").strip()
    owner_id = (owner_id or "").strip()
    if not artifact_id or not owner_id:
        raise ValueError("artifact id and owner id are required")
    payload = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        row = conn.execute(
            "SELECT owner_id FROM web_artifact_owners WHERE kind=? AND artifact_id=?",
            (kind, artifact_id),
        ).fetchone()
        if row is not None and row["owner_id"] != owner_id:
            raise ValueError("web artifact already belongs to another owner")
        conn.execute(
            """
            INSERT INTO web_artifact_owners
                (kind, artifact_id, owner_id, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(kind, artifact_id) DO UPDATE SET
                metadata_json=excluded.metadata_json
            """,
            (kind, artifact_id, owner_id, payload, now),
        )


def owned_web_artifact(
    kind: str, artifact_id: str, owner_id: str,
) -> dict[str, Any] | None:
    """Return stored metadata only when the exact owner matches."""
    if kind not in _ALLOWED_KINDS or not artifact_id or not owner_id:
        return None
    if not WEB_ARTIFACT_DB.is_file():
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT metadata_json FROM web_artifact_owners
            WHERE kind=? AND artifact_id=? AND owner_id=?
            """,
            (kind, artifact_id, owner_id),
        ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["metadata_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        value = {}
    return value if isinstance(value, dict) else {}


def web_artifact_has_owner(kind: str, artifact_id: str) -> bool:
    """Whether an id is registered at all, without revealing who owns it."""
    if kind not in _ALLOWED_KINDS or not artifact_id or not WEB_ARTIFACT_DB.is_file():
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM web_artifact_owners WHERE kind=? AND artifact_id=?",
            (kind, artifact_id),
        ).fetchone()
    return row is not None


def _legacy_history_attachment(artifact_id: str, owner_id: str) -> dict[str, Any] | None:
    """Recover ownership for web uploads created before this index existed."""
    try:
        from core.history_store import HISTORY_DIR, _owner_of
        for fp in HISTORY_DIR.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if _owner_of(data) != owner_id:
                continue
            candidates = list(data.get("attachments") or [])
            for message in data.get("messages") or []:
                if isinstance(message, dict):
                    candidates.extend(message.get("attachments") or [])
            for item in candidates:
                if isinstance(item, dict) and item.get("id") == artifact_id:
                    metadata = {
                        key: item.get(key) for key in (
                            "id", "filename", "char_count", "ext", "media_type",
                            "multimodal_status", "element_count", "preview_url",
                        ) if key in item
                    }
                    register_web_artifact("attachment", artifact_id, owner_id, metadata)
                    return metadata
    except OSError:
        pass
    return None


def owned_web_attachment(artifact_id: str, owner_id: str) -> dict[str, Any] | None:
    """Resolve a web upload for its owner, including safe legacy migration.

    Legacy ``local`` files are accepted only in auth-disabled local mode by the
    caller; authenticated production users must have an indexed or history-
    referenced attachment.  This prevents a guessed UUID from becoming a
    filesystem read primitive.
    """
    metadata = owned_web_artifact("attachment", artifact_id, owner_id)
    if metadata is not None:
        return metadata
    return _legacy_history_attachment(artifact_id, owner_id)


def owned_web_export(filename: str, owner_id: str) -> bool:
    """Check export ownership; migrate references from old history records."""
    if owned_web_artifact("export", filename, owner_id) is not None:
        return True
    try:
        from core.history_store import HISTORY_DIR, _owner_of
        for fp in HISTORY_DIR.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if _owner_of(data) != owner_id:
                continue
            raw = json.dumps(data, ensure_ascii=False)
            if filename in raw:
                register_web_artifact("export", filename, owner_id)
                return True
    except OSError:
        pass
    return False
