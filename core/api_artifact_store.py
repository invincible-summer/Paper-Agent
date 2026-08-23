"""Content-addressed artifacts for the OpenAI-compatible API channel only."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from core.api_storage_store import ApiStorageStore
from core.storage_context import StorageContext

_ALIAS_SAFE = re.compile(r"[^\w\-().\u3400-\u9fff]+", re.UNICODE)


@dataclass(frozen=True)
class ApiArtifact:
    id: str
    relative_path: str
    path: Path
    logical_name: str
    mime_type: str
    size_bytes: int
    category: str
    scope: str
    public_alias: str | None = None


class ApiArtifactStore:
    def __init__(self, storage: ApiStorageStore | None = None):
        self.context = storage.context if storage else StorageContext.openai_api()
        self.storage = storage or ApiStorageStore(self.context)
        self.storage.initialize()

    def _secret(self) -> bytes:
        return self.storage.get_or_create_secret("artifact_hmac")

    @staticmethod
    def _safe_display_name(name: str, fallback: str = "artifact") -> str:
        bare = Path(name or "").name.strip()
        cleaned = _ALIAS_SAFE.sub("_", bare).strip("._")
        return (cleaned[:180] or fallback)

    def _private_id(self, session_id: str, digest: str, category: str) -> str:
        value = f"{session_id}\0{category}\0{digest}".encode()
        return hmac.new(self._secret(), value, hashlib.sha256).hexdigest()

    def _atomic_copy(self, source: Path, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        tmp = self.context.temp_dir / f"artifact-{uuid.uuid4().hex}.tmp"
        with source.open("rb") as src, tmp.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, destination)
        os.chmod(destination, 0o600)
        return destination.stat().st_size

    def save_file(
        self,
        source: Path,
        *,
        category: str,
        scope: str,
        logical_name: str,
        mime_type: str,
        ttl_seconds: int,
        public_alias: str | None = None,
        unique: bool = False,
    ) -> ApiArtifact:
        if category == "public_pdf" or (category == "element_asset" and scope == "public"):
            raise RuntimeError(
                "network-paper PDF and public element artifacts are retired; "
                "use a session-private user upload"
            )
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        hex_digest = digest.hexdigest()
        if scope == "public":
            artifact_key = hex_digest
        else:
            artifact_key = self._private_id(scope, hex_digest, category)
        if unique:
            artifact_key = f"{artifact_key}_{uuid.uuid4().hex}"
        suffix = Path(logical_name).suffix.lower()
        relative = Path("blobs") / category / artifact_key[:2] / f"{artifact_key}{suffix}"
        destination = self.context.resolve_relative(relative)
        if not destination.is_file():
            self._atomic_copy(source, destination)
        now = time.time()
        artifact_id = f"{category}_{artifact_key}"
        with self.storage.connect() as conn:
            conn.execute(
                "INSERT INTO api_artifacts"
                "(id, content_hash, scope, category, privacy_class, logical_name, public_alias, mime_type, relative_path, size_bytes, created_at, last_accessed_at, expires_at, protected_until, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')"
                " ON CONFLICT(id) DO UPDATE SET last_accessed_at=excluded.last_accessed_at, expires_at=MAX(api_artifacts.expires_at, excluded.expires_at)",
                (artifact_id, hex_digest, scope, category,
                 "public" if scope == "public" else "private",
                 self._safe_display_name(logical_name), public_alias, mime_type,
                 str(relative), size, now, now, now + ttl_seconds, now + 3600),
            )
            conn.commit()
        return ApiArtifact(
            id=artifact_id, relative_path=str(relative), path=destination,
            logical_name=self._safe_display_name(logical_name), mime_type=mime_type,
            size_bytes=size, category=category, scope=scope, public_alias=public_alias,
        )

    def save_bytes(self, data: bytes, **kwargs) -> ApiArtifact:
        tmp = self.context.temp_dir / f"bytes-{uuid.uuid4().hex}.tmp"
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        try:
            return self.save_file(tmp, **kwargs)
        finally:
            tmp.unlink(missing_ok=True)

    def save_private_upload(
        self, source: Path, *, session_id: str, logical_name: str, mime_type: str,
        category: str = "upload",
    ) -> ApiArtifact:
        policy = self.storage.get_policy()
        artifact = self.save_file(
            source, category=category, scope=session_id, logical_name=logical_name,
            mime_type=mime_type, ttl_seconds=policy.upload_ttl_seconds,
        )
        now = time.time()
        with self.storage.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO api_session_artifacts(session_id, artifact_id, relation, created_at) VALUES (?, ?, ?, ?)",
                (session_id, artifact.id, category, now),
            )
            conn.commit()
        return artifact

    def save_public_pdf(self, source: Path, *, logical_name: str) -> ApiArtifact:
        """Compatibility boundary for retired network-paper PDFs.

        Public network-paper PDF artifacts are no longer a supported runtime
        capability; keeping this explicit failure avoids silently reviving old
        callers while preserving a useful migration error.
        """
        raise RuntimeError("network-paper PDF artifacts are retired; use a user-uploaded file")

    def save_export(
        self, source: Path, *, session_id: str, display_name: str, mime_type: str,
    ) -> ApiArtifact:
        suffix = Path(display_name).suffix.lower()
        stem = self._safe_display_name(Path(display_name).stem, "export")
        alias = f"{time.strftime('%Y%m%d_%H%M%S')}_{stem}_{uuid.uuid4().hex[:10]}{suffix}"
        artifact = self.save_file(
            source, category="export", scope=session_id, logical_name=display_name,
            mime_type=mime_type, ttl_seconds=self.storage.get_policy().export_ttl_seconds,
            public_alias=alias, unique=True,
        )
        now = time.time()
        with self.storage.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO api_session_artifacts(session_id, artifact_id, relation, created_at) VALUES (?, ?, 'export', ?)",
                (session_id, artifact.id, now),
            )
            conn.commit()
        return artifact

    def find_active(self, *, category: str, scope: str, logical_name: str) -> ApiArtifact | None:
        now = time.time()
        logical = self._safe_display_name(logical_name)
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT id, relative_path, logical_name, mime_type, size_bytes, category, scope, public_alias "
                "FROM api_artifacts WHERE category = ? AND scope = ? AND logical_name = ? "
                "AND status = 'active' AND expires_at > ? ORDER BY last_accessed_at DESC LIMIT 1",
                (category, scope, logical, now),
            ).fetchone()
        if not row:
            return None
        path = self.context.resolve_relative(row[1])
        if not path.is_file():
            return None
        return ApiArtifact(
            id=row[0], relative_path=row[1], path=path, logical_name=row[2],
            mime_type=row[3], size_bytes=row[4], category=row[5], scope=row[6],
            public_alias=row[7],
        )

    def has_public_alias(self, alias: str) -> bool:
        if Path(alias).name != alias or not alias:
            return False
        with self.storage.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM api_artifacts WHERE public_alias = ? LIMIT 1", (alias,)
            ).fetchone() is not None

    def resolve_public_alias(self, alias: str) -> ApiArtifact | None:
        if Path(alias).name != alias or not alias:
            return None
        now = time.time()
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT id, relative_path, logical_name, mime_type, size_bytes, category, scope, public_alias "
                "FROM api_artifacts WHERE public_alias = ? AND status = 'active' AND expires_at > ?",
                (alias, now),
            ).fetchone()
            if row:
                conn.execute("UPDATE api_artifacts SET last_accessed_at = ? WHERE id = ?", (now, row[0]))
                conn.commit()
        if not row:
            return None
        path = self.context.resolve_relative(row[1])
        if not path.is_file():
            return None
        return ApiArtifact(
            id=row[0], relative_path=row[1], path=path, logical_name=row[2],
            mime_type=row[3], size_bytes=row[4], category=row[5], scope=row[6],
            public_alias=row[7],
        )

    def path_for(self, artifact_id: str, *, session_id: str | None = None) -> Path | None:
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT relative_path, scope, status, expires_at FROM api_artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
        if not row or row[2] != "active" or (row[3] is not None and row[3] <= time.time()):
            return None
        if row[1] != "public" and session_id != row[1]:
            return None
        path = self.context.resolve_relative(row[0])
        return path if path.is_file() else None
