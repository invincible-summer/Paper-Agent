"""API-only retention, reconciliation and disk-pressure cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.api_storage_store import ApiStorageStore
from core.storage_context import StorageContext

_RECENT_TEMP_SECONDS = 60 * 60


@dataclass
class CleanupResult:
    mode: str
    reclaimed_bytes: int = 0
    deleted_artifacts: int = 0
    expired_sessions: int = 0
    expired_aliases: int = 0
    deleted_tmp: int = 0
    deleted_previews: int = 0
    actions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "reclaimed_bytes": self.reclaimed_bytes,
            "deleted_artifacts": self.deleted_artifacts,
            "expired_sessions": self.expired_sessions,
            "expired_aliases": self.expired_aliases, "deleted_tmp": self.deleted_tmp,
            "deleted_previews": self.deleted_previews, "actions": self.actions,
        }


class ApiStorageCleanup:
    def __init__(self, storage: ApiStorageStore | None = None,
                 disk_percent: Callable[[], float] | None = None):
        self.context = storage.context if storage else StorageContext.openai_api()
        self.storage = storage or ApiStorageStore(self.context)
        self.storage.initialize()
        self._disk_percent = disk_percent or self._real_disk_percent

    def _real_disk_percent(self) -> float:
        usage = shutil.disk_usage(self.context.root_dir)
        return round((usage.used / usage.total) * 100, 2) if usage.total else 0.0

    def _protected_session_ids(self, now: float) -> set[str]:
        with self.storage.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT session_id FROM api_inflight_operations WHERE expires_at > ?", (now,)
            ).fetchall()
        return {str(row[0]) for row in rows}

    def _artifact_rows(self, *, now: float, categories: tuple[str, ...] | None = None,
                       expired_only: bool = True, include_private: bool = False) -> list:
        where = ["status = 'active'", "(protected_until IS NULL OR protected_until <= ?)"]
        params: list = [now]
        if expired_only:
            where.append("expires_at IS NOT NULL AND expires_at <= ?")
            params.append(now)
        if not include_private:
            where.append("privacy_class != 'private'")
        if categories:
            where.append("category IN (%s)" % ",".join("?" for _ in categories))
            params.extend(categories)
        with self.storage.connect() as conn:
            return conn.execute(
                "SELECT id, relative_path, size_bytes, scope, category, privacy_class, created_at, last_accessed_at "
                "FROM api_artifacts WHERE " + " AND ".join(where) + " ORDER BY last_accessed_at ASC",
                params,
            ).fetchall()

    def _delete_artifact(self, row, result: CleanupResult, *, reason: str) -> None:
        artifact_id, rel, size, scope, category = row[0], row[1], int(row[2] or 0), row[3], row[4]
        now = time.time()
        try:
            path = self.context.resolve_relative(rel)
            if path.is_file():
                path.unlink()
                result.reclaimed_bytes += size
            with self.storage.connect() as conn:
                final_status = "evicted" if reason in {"pressure_rebuildable", "emergency_private"} else "deleted"
                conn.execute("UPDATE api_artifacts SET status=?, public_alias=NULL WHERE id = ?", (final_status, artifact_id))
                conn.execute("DELETE FROM api_session_artifacts WHERE artifact_id = ?", (artifact_id,))
                conn.commit()
            result.deleted_artifacts += 1
            result.actions.append({"kind": "artifact", "category": category, "reason": reason})
        except Exception:
            # Reconcile can retry physical/DB disagreement later; never escape root.
            return

    def _cleanup_traces(self, now: float, result: CleanupResult) -> None:
        with self.storage.connect() as conn:
            rows = conn.execute(
                "SELECT trace_id, relative_path FROM api_trace_records WHERE expires_at <= ?", (now,)
            ).fetchall()
        for row in rows:
            try:
                path = self.context.resolve_relative(row[1])
                if path.is_file():
                    result.reclaimed_bytes += path.stat().st_size
                    path.unlink()
                with self.storage.connect() as conn:
                    conn.execute("DELETE FROM api_trace_records WHERE trace_id=?", (row[0],))
                    conn.commit()
                result.actions.append({"kind": "trace", "reason": "expired"})
            except Exception:
                continue

    def _cleanup_tmp(self, now: float, result: CleanupResult) -> None:
        for path in self.context.temp_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                if now - path.stat().st_mtime < _RECENT_TEMP_SECONDS:
                    continue
                size = path.stat().st_size
                path.unlink()
                result.reclaimed_bytes += size
                result.deleted_tmp += 1
            except OSError:
                continue

    def _expire_sessions(self, now: float, result: CleanupResult) -> None:
        protected = self._protected_session_ids(now)
        with self.storage.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM api_sessions WHERE status='active' AND expires_at <= ?", (now,)
            ).fetchall()
        for row in rows:
            session_id = str(row[0])
            if session_id in protected:
                continue
            try:
                from tools.storage.vectorstore import VectorStore
                VectorStore(storage_context=self.context).delete_session(session_id)
            except Exception:
                pass
            with self.storage.connect() as conn:
                conn.execute("DELETE FROM api_session_aliases WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM api_session_artifacts WHERE session_id = ?", (session_id,))
                conn.execute(
                    "UPDATE api_sessions SET status='expired', checkpoint_blob=NULL, checkpoint_uncompressed_bytes=NULL WHERE id = ?",
                    (session_id,),
                )
                conn.commit()
            result.expired_sessions += 1

    def _cleanup_aliases_and_previews(self, now: float, result: CleanupResult) -> None:
        with self.storage.connect() as conn:
            result.expired_aliases += conn.execute(
                "DELETE FROM api_session_aliases WHERE expires_at <= ?", (now,)
            ).rowcount
            result.deleted_previews += conn.execute(
                "DELETE FROM api_cleanup_previews WHERE expires_at <= ? OR consumed_at IS NOT NULL", (now,)
            ).rowcount
            conn.execute("DELETE FROM api_inflight_operations WHERE expires_at <= ?", (now,))
            conn.commit()

    def _zero_ref_private(self, now: float, result: CleanupResult, *, emergency: bool = False) -> None:
        protected = self._protected_session_ids(now)
        with self.storage.connect() as conn:
            rows = conn.execute(
                "SELECT a.id, a.relative_path, a.size_bytes, a.scope, a.category, a.privacy_class, a.created_at, a.last_accessed_at "
                "FROM api_artifacts a LEFT JOIN api_session_artifacts r ON r.artifact_id=a.id "
                "WHERE a.status='active' AND a.privacy_class='private' "
                "AND (a.protected_until IS NULL OR a.protected_until <= ?) "
                "GROUP BY a.id HAVING COUNT(r.session_id)=0 ORDER BY a.last_accessed_at ASC",
                (now,),
            ).fetchall()
        for row in rows:
            if row[3] in protected:
                continue
            self._delete_artifact(row, result, reason="zero_ref")
        if not emergency:
            return
        # Emergency can evict inactive private uploads only after public/rebuildable data.
        cutoff = now - 24 * 3600
        with self.storage.connect() as conn:
            rows = conn.execute(
                "SELECT id, relative_path, size_bytes, scope, category, privacy_class, created_at, last_accessed_at "
                "FROM api_artifacts WHERE status='active' AND category IN ('upload','upload_sidecar') "
                "AND privacy_class='private' AND last_accessed_at <= ? "
                "AND (protected_until IS NULL OR protected_until <= ?) ORDER BY last_accessed_at ASC",
                (cutoff, now),
            ).fetchall()
        for row in rows:
            if row[3] not in protected:
                self._delete_artifact(row, result, reason="emergency_private")

    def _evict_rebuildable(self, now: float, result: CleanupResult) -> None:
        rows = self._artifact_rows(
            now=now, expired_only=False, include_private=True,
            categories=("public_pdf", "element_asset", "export"),
        )
        protected = self._protected_session_ids(now)
        for row in rows:
            if row[3] in protected:
                continue
            self._delete_artifact(row, result, reason="pressure_rebuildable")
        # API semantic/VLM caches are rebuildable and entirely isolated.
        try:
            from tools.storage.database import Database
            db = Database(storage_context=self.context)
            db.conn.execute("DELETE FROM vision_cache")
            db.conn.commit()
            db.close()
        except Exception:
            pass
        # Chroma is rebuildable from remaining Checkpoints/artifacts. Only API root is removed.
        try:
            if self.context.chroma_dir.exists() and not self._protected_session_ids(now):
                shutil.rmtree(self.context.chroma_dir)
                self.context.chroma_dir.mkdir(mode=0o700)
        except OSError:
            pass

    def reconcile(self, result: CleanupResult | None = None) -> CleanupResult:
        result = result or CleanupResult(mode="reconcile")
        now = time.time()
        with self.storage.connect() as conn:
            rows = conn.execute("SELECT id, relative_path FROM api_artifacts WHERE status='active'").fetchall()
        referenced: set[Path] = set()
        for row in rows:
            try:
                path = self.context.resolve_relative(row[1])
                referenced.add(path)
                if not path.is_file():
                    with self.storage.connect() as conn:
                        conn.execute("UPDATE api_artifacts SET status='missing', public_alias=NULL WHERE id=?", (row[0],))
                        conn.commit()
            except Exception:
                continue
        for path in self.context.blob_dir.rglob("*"):
            if not path.is_file() or path in referenced:
                continue
            try:
                if now - path.stat().st_mtime >= _RECENT_TEMP_SECONDS:
                    result.reclaimed_bytes += path.stat().st_size
                    path.unlink()
                    result.actions.append({"kind": "orphan", "reason": "reconcile"})
            except OSError:
                continue
        return result

    def run(self, *, mode: str = "scheduled", reconcile: bool = False,
            force_emergency: bool = False) -> CleanupResult:
        now = time.time()
        before = self._disk_percent()
        result = CleanupResult(mode=mode)
        run_id = uuid.uuid4().hex
        with self.storage.connect() as conn:
            conn.execute(
                "INSERT INTO api_cleanup_runs(id, mode, started_at, status, disk_percent_before) VALUES (?, ?, ?, 'running', ?)",
                (run_id, mode, now, before),
            )
            conn.commit()
        try:
            self._cleanup_aliases_and_previews(now, result)
            self._cleanup_tmp(now, result)
            self._cleanup_traces(now, result)
            self._expire_sessions(now, result)
            for row in self._artifact_rows(now=now, expired_only=True, include_private=True):
                if row[3] not in self._protected_session_ids(now):
                    self._delete_artifact(row, result, reason="expired")
            self._zero_ref_private(now, result)
            policy = self.storage.get_policy()
            current = self._disk_percent()
            if current >= policy.pressure_threshold_percent:
                self._evict_rebuildable(now, result)
            if current >= policy.critical_threshold_percent and (policy.critical_strategy == "emergency_evict" or force_emergency):
                self._zero_ref_private(now, result, emergency=True)
            if reconcile:
                self.reconcile(result)
            after = self._disk_percent()
            with self.storage.connect() as conn:
                conn.execute(
                    "UPDATE api_runtime_state SET disk_percent=?, last_cleanup_at=?, updated_at=? WHERE id=1",
                    (after, time.time(), time.time()),
                )
                conn.execute(
                    "UPDATE api_cleanup_runs SET finished_at=?, status='success', disk_percent_after=?, reclaimed_bytes=?, counters_json=? WHERE id=?",
                    (time.time(), after, result.reclaimed_bytes, json.dumps(result.to_dict(), ensure_ascii=False), run_id),
                )
                conn.commit()
            return result
        except Exception as exc:
            with self.storage.connect() as conn:
                conn.execute(
                    "UPDATE api_cleanup_runs SET finished_at=?, status='error', error_code=? WHERE id=?",
                    (time.time(), type(exc).__name__, run_id),
                )
                conn.commit()
            raise

    def create_preview(self, *, action: str, created_by: str, payload: dict | None = None) -> dict:
        now = time.time()
        with self.storage.connect() as conn:
            expired_artifacts = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes),0) FROM api_artifacts "
                "WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ? "
                "AND (protected_until IS NULL OR protected_until <= ?)", (now, now)
            ).fetchone()
            expired_sessions = conn.execute(
                "SELECT COUNT(*) FROM api_sessions WHERE status='active' AND expires_at <= ?", (now,)
            ).fetchone()[0]
        result = CleanupResult(mode="preview")
        result.deleted_artifacts = int(expired_artifacts[0])
        result.reclaimed_bytes = int(expired_artifacts[1])
        result.expired_sessions = int(expired_sessions)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        policy = self.storage.get_policy()
        with self.storage.connect() as conn:
            conn.execute(
                "INSERT INTO api_cleanup_previews(token_hash, action, payload_json, policy_version, created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_hash, action, json.dumps({"preview": result.to_dict(), "payload": payload or {}}, ensure_ascii=False), policy.version,
                 created_by, now, now + 600),
            )
            conn.commit()
        return {"token": token, "expires_at": now + 600, "preview": result.to_dict()}

    def consume_preview(self, token: str, *, action: str, expected_policy_version: int) -> dict:
        digest = hashlib.sha256((token or "").encode()).hexdigest()
        now = time.time()
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT action, payload_json, policy_version FROM api_cleanup_previews "
                "WHERE token_hash=? AND expires_at>? AND consumed_at IS NULL",
                (digest, now),
            ).fetchone()
            if row is None or row[0] != action:
                raise ValueError("invalid or expired preview token")
            if int(row[2]) != int(expected_policy_version):
                raise ValueError("preview policy version changed")
            conn.execute("UPDATE api_cleanup_previews SET consumed_at=? WHERE token_hash=?", (now, digest))
            conn.commit()
        return json.loads(row[1])

    def execute_preview(self, token: str) -> CleanupResult:
        digest = hashlib.sha256((token or "").encode()).hexdigest()
        now = time.time()
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT action, policy_version FROM api_cleanup_previews WHERE token_hash=? AND expires_at>? AND consumed_at IS NULL",
                (digest, now),
            ).fetchone()
            if row is None:
                raise ValueError("invalid or expired cleanup preview token")
            if int(row[1]) != self.storage.get_policy().version:
                raise ValueError("cleanup preview policy version changed")
            conn.execute("UPDATE api_cleanup_previews SET consumed_at=? WHERE token_hash=?", (now, digest))
            conn.commit()
        if row[0] not in {"immediate_cleanup", "emergency_evict"}:
            raise ValueError("preview token is not executable as cleanup")
        return self.run(
            mode=f"execute:{row[0]}", reconcile=True,
            force_emergency=row[0] == "emergency_evict",
        )
