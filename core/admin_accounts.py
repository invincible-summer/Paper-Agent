"""Administrator account-storage census and per-account deletion.

Covers BOTH sides of the product:
  * web accounts (`history_record/*.json`, `data/uploads/<attachment-id>.*`,
    trace JSONL, session-scoped Chroma data) — each record carries user_id;
  * OpenAI-compatible credentials (`agent_api_keys`) — API sessions, private
    uploads/exports and their checkpoint blobs, keyed by credential_id.

Shared/rebuildable caches are deliberately NOT attributed to an account. Legacy
network-paper cache retirement is handled by ``core.remote_fulltext_retirement``
and its CLI, not by account deletion.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from core.history_store import _owner_of, derive_title
from core.storage_context import StorageContext

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _root(project_root: str | Path | None = None) -> Path:
    return Path(project_root).expanduser().resolve() if project_root else _PROJECT_ROOT


def _safe_account_id(value: str) -> str:
    value = (value or "").strip()
    if not _ACCOUNT_ID_RE.fullmatch(value):
        raise ValueError("账号 id 不合法")
    return value


def _secure_unlink(path: Path) -> tuple[bool, int]:
    """Overwrite, fsync, then unlink one file (best-effort unrecoverable).

    Normal ``unlink`` only removes the directory entry; an attacker with raw
    disk access could still carve the contents. Overwriting the whole file
    before unlink removes that recovery path for files this process owns.
    """
    try:
        if path.is_symlink():
            path.unlink()
            return True, 0
        size = path.stat().st_size
    except OSError:
        return False, 0
    if size > 0:
        try:
            zero = b"\0" * min(1024 * 1024, size)
            with path.open("r+b") as stream:
                written = 0
                while written < size:
                    chunk = zero[: size - written]
                    stream.write(chunk)
                    written += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            # Still try to unlink below; freeing the directory entry is the
            # most important guarantee.
            pass
    try:
        path.unlink()
        return True, size
    except OSError:
        return False, 0


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------

def _iter_histories(project_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    record_dir = project_root / "history_record"
    out: list[tuple[Path, dict[str, Any]]] = []
    if not record_dir.is_dir():
        return out
    for fp in sorted(record_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            out.append((fp, data))
    return out


def _attachment_file_sizes(project_root: Path, attachment_id: str) -> list[tuple[Path, int]]:
    if not re.fullmatch(r"[a-f0-9]{32}", attachment_id or ""):
        return []
    uploads = project_root / "data" / "uploads"
    out: list[tuple[Path, int]] = []
    for name in (
        f"{attachment_id}.txt", f"{attachment_id}.pdf", f"{attachment_id}.docx",
        f"{attachment_id}.png", f"{attachment_id}.jpg", f"{attachment_id}.jpeg",
        f"{attachment_id}.webp", f"{attachment_id}.tex", f"{attachment_id}.md",
        f"{attachment_id}.bib",
    ):
        fp = uploads / name
        if fp.is_file():
            try:
                out.append((fp, fp.stat().st_size))
            except OSError:
                continue
    return out


def _trace_file(project_root: Path, trace_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", trace_id or ""):
        return project_root / "__invalid__"
    return project_root / "history_record" / "trace" / f"trace_{trace_id}.jsonl"


def _paper_ids(data: dict) -> int:
    return len(data.get("papers") or []) + len(data.get("candidates") or [])


def _web_account_rows(project_root: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    histories = _iter_histories(project_root)
    for fp, data in histories:
        owner = _owner_of(data)
        row = rows.setdefault(owner, {
            "account_id": owner, "account_type": "web_user",
            "label": owner, "role": "local", "disabled": False,
            "history_count": 0, "history_bytes": 0, "history_files": [],
            "paper_count": 0, "attachment_ids": set(), "upload_count": 0,
            "upload_bytes": 0, "trace_ids": set(), "trace_count": 0,
            "trace_bytes": 0, "session_ids": set(),
        })
        row["history_count"] += 1
        try:
            size = fp.stat().st_size
        except OSError:
            size = 0
        row["history_bytes"] += size
        row["history_files"].append(fp.name)
        row["paper_count"] += _paper_ids(data)
        session_id = str(data.get("session_id") or "")
        if session_id:
            row["session_ids"].add(session_id)
        for att in data.get("attachments") or []:
            if isinstance(att, dict) and att.get("id"):
                row["attachment_ids"].add(str(att["id"]))
        for tid in data.get("trace_ids") or []:
            tid = str(tid or "")
            if tid:
                row["trace_ids"].add(tid)

    for row in rows.values():
        for aid in row["attachment_ids"]:
            for _fp, size in _attachment_file_sizes(project_root, str(aid)):
                row["upload_count"] += 1
                row["upload_bytes"] += size
        for tid in row["trace_ids"]:
            tf = _trace_file(project_root, str(tid))
            if tf.is_file():
                try:
                    row["trace_count"] += 1
                    row["trace_bytes"] += tf.stat().st_size
                except OSError:
                    pass
    return rows


def _api_session_rows(store, credential_id: str) -> list:
    with store.connect() as conn:
        return conn.execute(
            "SELECT id, rag_session_id, checkpoint_uncompressed_bytes FROM api_sessions "
            "WHERE credential_id = ?", (credential_id,),
        ).fetchall()


def _api_private_artifacts(store, session_ids: list[str]) -> list:
    if not session_ids:
        return []
    placeholders = ",".join("?" * len(session_ids))
    with store.connect() as conn:
        return conn.execute(
            "SELECT a.id, a.relative_path, a.size_bytes, a.category, a.status "
            "FROM api_artifacts a JOIN api_session_artifacts r ON r.artifact_id = a.id "
            f"WHERE r.session_id IN ({placeholders}) AND a.status = 'active'",
            session_ids,
        ).fetchall()


def _api_account_rows(api_root: Path) -> dict[str, dict]:
    from core.api_storage_store import ApiStorageStore
    from core.user_store import list_agent_api_keys

    context = StorageContext.openai_api(root_dir=api_root)
    store = ApiStorageStore(context)
    rows: dict[str, dict] = {}
    try:
        store.initialize()
    except Exception as e:  # noqa: BLE001
        logger.warning("API storage initialize skipped: %s", e)
    keys = list_agent_api_keys()
    for key in keys:
        key_id = str(key["id"])
        row = {
            "account_id": key_id, "account_type": "api_key",
            "label": f"{key['name']}（{key['key_prefix']}…{key['key_suffix']}）",
            "role": "agent_key", "disabled": key.get("revoked_at") is not None,
            "history_count": 0, "history_bytes": 0, "history_files": [],
            "paper_count": 0, "attachment_ids": set(), "upload_count": 0,
            "upload_bytes": 0, "trace_ids": set(), "trace_count": 0,
            "trace_bytes": 0, "session_ids": set(),
            "api_sessions": 0, "api_checkpoint_bytes": 0,
            "api_private_artifacts": 0, "api_private_bytes": 0,
        }
        try:
            sessions = _api_session_rows(store, key_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("API session query skipped for %s: %s", key_id, e)
            sessions = []
        session_ids = [str(s[0]) for s in sessions]
        row["api_sessions"] = len(session_ids)
        row["api_checkpoint_bytes"] = int(sessions[0][2] or 0) if sessions and sessions[0][2] else 0
        row["session_ids"] = set(session_ids)
        try:
            artifacts = _api_private_artifacts(store, session_ids)
        except Exception as e:  # noqa: BLE001
            logger.debug("API artifact query skipped for %s: %s", key_id, e)
            artifacts = []
        row["api_private_artifacts"] = len(artifacts)
        row["api_private_bytes"] = sum(int(a[2] or 0) for a in artifacts)
        rows[key_id] = row
    return rows


def list_accounts_data(project_root: str | Path | None = None,
                       api_root: str | Path | None = None) -> dict:
    """Return per-account storage usage for the admin console."""
    root = _root(project_root)
    web_rows = _web_account_rows(root)
    from core.user_store import list_users
    # Every real account appears even when it has no history yet.
    for user in list_users():
        uid = str(user["id"])
        if uid not in web_rows:
            web_rows[uid] = {
                "account_id": uid, "account_type": "web_user",
                "label": user.get("display_name") or user.get("username") or uid,
                "role": user.get("role", "user"),
                "disabled": bool(user.get("disabled")),
                "history_count": 0, "history_bytes": 0, "history_files": [],
                "paper_count": 0, "attachment_ids": set(), "upload_count": 0,
                "upload_bytes": 0, "trace_ids": set(), "trace_count": 0,
                "trace_bytes": 0, "session_ids": set(),
            }
        else:
            user_map = {str(u["id"]): u for u in list_users()}
            meta = user_map.get(uid, {})
            web_rows[uid]["label"] = meta.get("display_name") or meta.get("username") or uid
            web_rows[uid]["role"] = meta.get("role", "local")
            web_rows[uid]["disabled"] = bool(meta.get("disabled"))

    api_rows = _api_account_rows(Path(api_root).expanduser().resolve() if api_root
                                  else StorageContext.openai_api().root_dir)
    items = list(web_rows.values()) + list(api_rows.values())
    for item in items:
        item["attachment_count"] = len(item.pop("attachment_ids"))
        item["trace_id_count"] = len(item.pop("trace_ids"))
        item["session_count"] = len(item.pop("session_ids"))
    return {
        "items": items,
        "totals": {
            "history_bytes": sum(i["history_bytes"] for i in items),
            "upload_bytes": sum(i["upload_bytes"] for i in items),
            "trace_bytes": sum(i["trace_bytes"] for i in items),
            "api_private_bytes": sum(i.get("api_private_bytes", 0) for i in items),
        },
    }


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def _delete_web_vectors(project_root: Path, session_ids: set[str]) -> None:
    try:
        from tools.storage.vectorstore import VectorStore
        context = StorageContext.web(project_root=project_root)
        vs = VectorStore(storage_context=context)
        for session_id in session_ids:
            try:
                vs.delete_session(session_id)
            except Exception as e:  # noqa: BLE001
                logger.debug("vector session delete failed %s: %s", session_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("web vector cleanup skipped: %s", e)


def delete_web_account_data(account_id: str,
                            project_root: str | Path | None = None) -> dict:
    """Delete every history + upload + trace + session-vector owned by one web user."""
    account_id = _safe_account_id(account_id)
    root = _root(project_root)
    histories = _iter_histories(root)
    owned = [(fp, data) for fp, data in histories if _owner_of(data) == account_id]
    if not owned:
        return {"deleted": False, "reason": "account_not_found", "bytes": 0, "files": 0}

    other_attachment_ids: set[str] = set()
    other_trace_ids: set[str] = set()
    for fp, data in histories:
        if _owner_of(data) == account_id:
            continue
        for att in data.get("attachments") or []:
            if isinstance(att, dict) and att.get("id"):
                other_attachment_ids.add(str(att["id"]))
        for tid in data.get("trace_ids") or []:
            if tid:
                other_trace_ids.add(str(tid))

    session_ids: set[str] = set()
    deleted_bytes = 0
    deleted_files = 0
    for fp, data in owned:
        session_id = str(data.get("session_id") or "")
        if session_id:
            session_ids.add(session_id)
        for att in data.get("attachments") or []:
            aid = str((att or {}).get("id") or "")
            if aid in other_attachment_ids:
                continue
            for upload, _size in _attachment_file_sizes(root, aid):
                ok, size = _secure_unlink(upload)
                if ok:
                    deleted_bytes += size
                    deleted_files += 1
        for tid in data.get("trace_ids") or []:
            tid = str(tid or "")
            if tid in other_trace_ids:
                continue
            tf = _trace_file(root, tid)
            if tf.is_file():
                ok, size = _secure_unlink(tf)
                if ok:
                    deleted_bytes += size
                    deleted_files += 1
        ok, size = _secure_unlink(fp)
        if ok:
            deleted_bytes += size
            deleted_files += 1

    _delete_web_vectors(root, session_ids)
    return {"deleted": True, "account_id": account_id, "bytes": deleted_bytes,
            "files": deleted_files, "histories": len(owned)}


def delete_api_key_data(key_id: str,
                        api_root: str | Path | None = None) -> dict:
    """Revoke an Agent API key and delete all sessions/files owned by it."""
    key_id = _safe_account_id(key_id)
    from core.api_storage_store import ApiStorageStore
    from core.user_store import list_agent_api_keys, revoke_agent_api_key

    keys = {str(k["id"]) for k in list_agent_api_keys()}
    if key_id not in keys:
        return {"deleted": False, "reason": "key_not_found", "bytes": 0, "files": 0}

    context = StorageContext.openai_api(root_dir=api_root)
    store = ApiStorageStore(context)
    store.initialize()
    sessions = _api_session_rows(store, key_id)
    session_ids = [str(s[0]) for s in sessions]
    artifacts = _api_private_artifacts(store, session_ids)

    # Session-scoped Chroma first (best-effort).
    try:
        from tools.storage.vectorstore import VectorStore
        vs = VectorStore(storage_context=context)
        for s in sessions:
            rag_id = str(s[1] or "")
            if rag_id:
                try:
                    vs.delete_session(rag_id)
                except Exception as e:  # noqa: BLE001
                    logger.debug("API vector session delete failed %s: %s", rag_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("API vector cleanup skipped: %s", e)

    deleted_bytes = 0
    deleted_files = 0
    artifact_ids: list[str] = []
    for art in artifacts:
        art_id, rel, size, _category, _status = art
        artifact_ids.append(str(art_id))
        try:
            path = context.resolve_relative(str(rel))
            if path.is_file():
                ok, freed = _secure_unlink(path)
                if ok:
                    deleted_bytes += int(size or 0)
                    deleted_files += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("API artifact unlink failed %s: %s", art_id, e)
    with store.connect() as conn:
        if artifact_ids:
            placeholders = ",".join("?" * len(artifact_ids))
            conn.execute(
                f"UPDATE api_artifacts SET status='deleted', public_alias=NULL "
                f"WHERE id IN ({placeholders})", artifact_ids)
        for s in sessions:
            if s[2]:
                deleted_bytes += int(s[2] or 0)
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("DELETE FROM api_sessions WHERE credential_id = ?", (key_id,))
        conn.commit()
        try:
            # Reclaim freed pages so "彻底删除" also shrinks the DB file.
            conn.execute("VACUUM")
        except Exception as e:  # noqa: BLE001
            logger.debug("API state DB vacuum skipped: %s", e)
    deleted_files += len(sessions)

    revoked = revoke_agent_api_key(key_id)
    return {"deleted": True, "account_id": key_id, "bytes": deleted_bytes,
            "files": deleted_files, "sessions": len(sessions),
            "artifacts": len(artifact_ids), "revoked": revoked}
