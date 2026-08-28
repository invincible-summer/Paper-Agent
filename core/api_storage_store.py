"""SQLite foundation for isolated OpenAI-compatible API storage.

This module intentionally does not create files at import time.  Callers must
construct an ``ApiStorageStore`` and invoke ``initialize`` explicitly.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from core.storage_context import StorageContext, StoragePathError

SCHEMA_VERSION = 9

MIN_API_UPLOAD_BYTES = 1 * 1024 * 1024
MAX_API_UPLOAD_BYTES = 200 * 1024 * 1024
DEFAULT_API_UPLOAD_BYTES = MAX_API_UPLOAD_BYTES

DISPLAY_POLICY_CACHE_TTL_SECONDS = 5.0

class ApiStorageError(RuntimeError):
    pass


class PolicyVersionConflict(ApiStorageError):
    """Optimistic policy update lost a race."""


@dataclass(frozen=True)
class ApiDisplayPolicy:
    """清小搭 (/v1) markdown-card display policy, admin-editable.

    tool_cards_enabled: emit the per-tool one-line status cards into
    delta.content (the mandated search-results table and file attachments
    stay on regardless of this switch).
    tool_error_cards_enabled: additionally allow error-status cards (the
    ⚠️ one-liners, e.g. tool timeouts) into delta.content. Off by default:
    errors keep steering the model through the unchanged tool-result
    channel; only the user-facing display is suppressed.
    skill_card_enabled: emit the in-content skill-loading line.
    research_map_*_enabled: independently select SVG attachment, Mermaid
    content, self-contained HTML attachment, and Markdown relation listing.
    bibtex_export_mode: how citation_export BibTeX files are attached on the
    /v1 channel — "md_only" (default; 清小搭 cannot download .bib, so the
    raw BibTeX text rides a .md attachment), "bib_and_md" (both files), or
    "bib_only" (legacy .bib-only behavior). A trailing notice line is
    appended to the formal output whenever the .md copy is exported.
    """
    tool_cards_enabled: bool = True
    tool_error_cards_enabled: bool = False
    skill_card_enabled: bool = True
    research_map_svg_enabled: bool = True
    research_map_mermaid_enabled: bool = False
    research_map_html_enabled: bool = False
    research_map_markdown_enabled: bool = False
    bibtex_export_mode: str = "md_only"
    version: int = 1
    updated_by: str = "bootstrap"
    updated_at: float = 0.0


_BIBTEX_EXPORT_MODES = frozenset({"bib_and_md", "md_only", "bib_only"})


_display_policy_cache: dict[str, tuple[float, ApiDisplayPolicy]] = {}


def reset_display_policy_cache() -> None:
    """Clear the single-worker display-policy read cache (tests/admin refresh)."""
    _display_policy_cache.clear()


@dataclass(frozen=True)
class ApiStoragePolicy:
    preset: str = "balanced"
    session_ttl_seconds: int = 7 * 24 * 60 * 60
    upload_ttl_seconds: int = 7 * 24 * 60 * 60
    max_upload_bytes: int = DEFAULT_API_UPLOAD_BYTES
    export_ttl_seconds: int = 24 * 60 * 60
    cache_ttl_seconds: int = 90 * 24 * 60 * 60
    trace_ttl_seconds: int = 7 * 24 * 60 * 60
    cleanup_interval_minutes: int = 60
    observe_threshold_percent: int = 75
    pressure_threshold_percent: int = 85
    critical_threshold_percent: int = 95
    hard_stop_threshold_percent: int = 98
    pressure_strategy: str = "continuous_evict"
    critical_strategy: str = "pause_heavy"
    trace_mode: str = "off"
    version: int = 1
    updated_by: str = "bootstrap"
    updated_at: float = 0.0


DEFAULT_POLICY: dict[str, Any] = {
    key: value
    for key, value in asdict(ApiStoragePolicy()).items()
    if key not in {"version", "updated_by", "updated_at"}
}
POLICY_PRESETS: dict[str, dict[str, Any]] = {
    "privacy": DEFAULT_POLICY | {
        "preset": "privacy",
        "session_ttl_seconds": 2 * 60 * 60,
        "upload_ttl_seconds": 2 * 60 * 60,
        "export_ttl_seconds": 2 * 60 * 60,
        "cache_ttl_seconds": 30 * 24 * 60 * 60,
        "trace_mode": "off",
    },
    "balanced": DEFAULT_POLICY,
    "performance": DEFAULT_POLICY | {
        "preset": "performance",
        "session_ttl_seconds": 30 * 24 * 60 * 60,
        "upload_ttl_seconds": 30 * 24 * 60 * 60,
        "export_ttl_seconds": 7 * 24 * 60 * 60,
        "cache_ttl_seconds": 180 * 24 * 60 * 60,
        "trace_mode": "metadata",
        "trace_ttl_seconds": 7 * 24 * 60 * 60,
    },
}

_POLICY_COLUMNS = frozenset(DEFAULT_POLICY)
_POLICY_ENUMS = {
    "preset": {"privacy", "balanced", "performance", "archive", "custom"},
    "pressure_strategy": {"continuous_evict"},
    "critical_strategy": {"pause_heavy", "emergency_evict"},
    "trace_mode": {"off", "metadata", "full"},
}
_TTL_COLUMNS = {
    "session_ttl_seconds", "upload_ttl_seconds", "export_ttl_seconds",
    "cache_ttl_seconds", "trace_ttl_seconds",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_storage_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    preset TEXT NOT NULL,
    session_ttl_seconds INTEGER NOT NULL CHECK (session_ttl_seconds > 0),
    upload_ttl_seconds INTEGER NOT NULL CHECK (upload_ttl_seconds > 0),
    max_upload_bytes INTEGER NOT NULL DEFAULT 209715200
        CHECK (max_upload_bytes >= 1048576 AND max_upload_bytes <= 209715200),
    export_ttl_seconds INTEGER NOT NULL CHECK (export_ttl_seconds > 0),
    public_pdf_ttl_seconds INTEGER NOT NULL DEFAULT 259200 CHECK (public_pdf_ttl_seconds > 0),
    cache_ttl_seconds INTEGER NOT NULL CHECK (cache_ttl_seconds > 0),
    trace_ttl_seconds INTEGER NOT NULL CHECK (trace_ttl_seconds > 0),
    cleanup_interval_minutes INTEGER NOT NULL CHECK (cleanup_interval_minutes > 0),
    observe_threshold_percent INTEGER NOT NULL,
    pressure_threshold_percent INTEGER NOT NULL,
    critical_threshold_percent INTEGER NOT NULL,
    hard_stop_threshold_percent INTEGER NOT NULL,
    pressure_strategy TEXT NOT NULL,
    critical_strategy TEXT NOT NULL,
    trace_mode TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    updated_by TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_sessions (
    id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL,
    openai_user_hash TEXT,
    rag_session_id TEXT NOT NULL,
    checkpoint_blob BLOB,
    checkpoint_schema_version INTEGER,
    checkpoint_uncompressed_bytes INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_accessed_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_api_sessions_expiry ON api_sessions(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_api_sessions_credential ON api_sessions(credential_id, last_accessed_at);

CREATE TABLE IF NOT EXISTS api_session_aliases (
    lookup_hash TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES api_sessions(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL,
    ambiguous INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),
    created_at REAL NOT NULL,
    PRIMARY KEY (lookup_hash, session_id)
);
CREATE INDEX IF NOT EXISTS idx_api_aliases_expiry ON api_session_aliases(expires_at);

CREATE TABLE IF NOT EXISTS api_artifacts (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    category TEXT NOT NULL,
    privacy_class TEXT NOT NULL,
    logical_name TEXT NOT NULL,
    public_alias TEXT UNIQUE,
    mime_type TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    created_at REAL NOT NULL,
    last_accessed_at REAL NOT NULL,
    expires_at REAL,
    protected_until REAL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_api_artifacts_cleanup ON api_artifacts(status, expires_at, category);
CREATE INDEX IF NOT EXISTS idx_api_artifacts_hash ON api_artifacts(content_hash, scope, category);

CREATE TABLE IF NOT EXISTS api_session_artifacts (
    session_id TEXT NOT NULL REFERENCES api_sessions(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL REFERENCES api_artifacts(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, artifact_id, relation)
);

CREATE TABLE IF NOT EXISTS api_cleanup_runs (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    disk_percent_before REAL,
    disk_percent_after REAL,
    reclaimed_bytes INTEGER NOT NULL DEFAULT 0,
    counters_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS api_cleanup_previews (
    token_hash TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_api_cleanup_previews_expiry ON api_cleanup_previews(expires_at);

CREATE TABLE IF NOT EXISTS api_trace_aggregates (
    day TEXT PRIMARY KEY,
    requests INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_latency_ms REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_trace_records (
    trace_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_trace_expiry ON api_trace_records(expires_at);

CREATE TABLE IF NOT EXISTS api_inflight_operations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES api_sessions(id) ON DELETE CASCADE,
    operation TEXT NOT NULL,
    started_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_inflight_expiry ON api_inflight_operations(expires_at);

CREATE TABLE IF NOT EXISTS api_runtime_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heavy_writes_paused INTEGER NOT NULL DEFAULT 0 CHECK (heavy_writes_paused IN (0, 1)),
    pause_reason TEXT,
    disk_percent REAL,
    last_cleanup_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_display_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tool_cards_enabled INTEGER NOT NULL DEFAULT 1 CHECK (tool_cards_enabled IN (0, 1)),
    tool_error_cards_enabled INTEGER NOT NULL DEFAULT 0 CHECK (tool_error_cards_enabled IN (0, 1)),
    skill_card_enabled INTEGER NOT NULL DEFAULT 1 CHECK (skill_card_enabled IN (0, 1)),
    research_map_svg_enabled INTEGER NOT NULL DEFAULT 1 CHECK (research_map_svg_enabled IN (0, 1)),
    research_map_mermaid_enabled INTEGER NOT NULL DEFAULT 0 CHECK (research_map_mermaid_enabled IN (0, 1)),
    research_map_html_enabled INTEGER NOT NULL DEFAULT 0 CHECK (research_map_html_enabled IN (0, 1)),
    research_map_markdown_enabled INTEGER NOT NULL DEFAULT 0 CHECK (research_map_markdown_enabled IN (0, 1)),
    bibtex_export_mode TEXT NOT NULL DEFAULT 'md_only'
        CHECK (bibtex_export_mode IN ('bib_and_md', 'md_only', 'bib_only')),
    version INTEGER NOT NULL CHECK (version > 0),
    updated_by TEXT NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (research_map_svg_enabled + research_map_mermaid_enabled + research_map_html_enabled >= 1)
);

CREATE TABLE IF NOT EXISTS api_secrets (
    name TEXT PRIMARY KEY,
    secret_blob BLOB NOT NULL,
    created_at REAL NOT NULL,
    rotated_at REAL
);
"""


class ApiStorageStore:
    def __init__(self, context: StorageContext):
        if context.channel != "openai_api":
            raise StoragePathError("ApiStorageStore requires an openai_api StorageContext")
        self.context = context
        self.db_path = context.state_db

    def initialize(self) -> None:
        self.context.ensure_layout()
        self._create_private_db_file()
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            now = time.time()
            display_policy_changed = False
            row = conn.execute(
                "SELECT schema_version FROM api_schema_meta WHERE id = 1"
            ).fetchone()
            if row is not None and int(row[0]) > SCHEMA_VERSION:
                raise ApiStorageError(
                    f"state.db schema {row[0]} is newer than supported {SCHEMA_VERSION}"
                )
            previous_version = int(row[0]) if row is not None else 0
            policy_columns = {
                str(column[1])
                for column in conn.execute("PRAGMA table_info(api_storage_policy)").fetchall()
            }
            if "max_upload_bytes" not in policy_columns:
                # v6: /v1 remote-file ingestion gets an administrator-controlled
                # limit. Existing databases inherit the 清小搭-compatible 200 MiB
                # default without changing their optimistic-lock policy version.
                conn.execute(
                    "ALTER TABLE api_storage_policy ADD COLUMN max_upload_bytes "
                    "INTEGER NOT NULL DEFAULT 209715200 "
                    "CHECK (max_upload_bytes >= 1048576 AND max_upload_bytes <= 209715200)"
                )
            display_columns = {
                str(column[1])
                for column in conn.execute("PRAGMA table_info(api_display_policy)").fetchall()
            }
            new_display_columns = {
                "research_map_svg_enabled", "research_map_mermaid_enabled",
                "research_map_html_enabled", "research_map_markdown_enabled",
            }
            if not new_display_columns.issubset(display_columns):
                display_policy_changed = True
                # v9 replaces the runtime renderer enum with four independent
                # booleans. Rebuilding removes the old column and installs the
                # cross-column "at least one graph" constraint.
                conn.execute("ALTER TABLE api_display_policy RENAME TO api_display_policy_pre_v9")
                conn.execute(
                    "CREATE TABLE api_display_policy ("
                    "id INTEGER PRIMARY KEY CHECK (id = 1), "
                    "tool_cards_enabled INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (tool_cards_enabled IN (0, 1)), "
                    "skill_card_enabled INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (skill_card_enabled IN (0, 1)), "
                    "research_map_svg_enabled INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (research_map_svg_enabled IN (0, 1)), "
                    "research_map_mermaid_enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (research_map_mermaid_enabled IN (0, 1)), "
                    "research_map_html_enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (research_map_html_enabled IN (0, 1)), "
                    "research_map_markdown_enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (research_map_markdown_enabled IN (0, 1)), "
                    "version INTEGER NOT NULL CHECK (version > 0), "
                    "updated_by TEXT NOT NULL, updated_at REAL NOT NULL, "
                    "CHECK (research_map_svg_enabled + research_map_mermaid_enabled + "
                    "research_map_html_enabled >= 1))"
                )
                if "preset" in display_columns:
                    # Pre-v7 presets had no map selector and therefore used the
                    # historical SVG + Markdown pair.
                    conn.execute(
                        "INSERT INTO api_display_policy(id, tool_cards_enabled, "
                        "skill_card_enabled, research_map_svg_enabled, "
                        "research_map_mermaid_enabled, research_map_html_enabled, "
                        "research_map_markdown_enabled, version, updated_by, updated_at) "
                        "SELECT id, CASE WHEN preset = 'off' THEN 0 ELSE 1 END, "
                        "skill_card_enabled, 1, 0, 0, 1, version, updated_by, updated_at "
                        "FROM api_display_policy_pre_v9"
                    )
                elif "research_map_render_strategy" in display_columns:
                    # v8 enum migration: legacy_svg and pretty_svg_markdown
                    # retain Markdown; pretty_svg remains SVG-only.
                    conn.execute(
                        "INSERT INTO api_display_policy(id, tool_cards_enabled, "
                        "skill_card_enabled, research_map_svg_enabled, "
                        "research_map_mermaid_enabled, research_map_html_enabled, "
                        "research_map_markdown_enabled, version, updated_by, updated_at) "
                        "SELECT id, tool_cards_enabled, skill_card_enabled, 1, 0, 0, "
                        "CASE WHEN research_map_render_strategy IN "
                        "('legacy_svg', 'pretty_svg_markdown') THEN 1 ELSE 0 END, "
                        "version, updated_by, updated_at FROM api_display_policy_pre_v9"
                    )
                else:
                    # v7 databases had the two card toggles only and used the
                    # historical SVG + Markdown output implicitly.
                    conn.execute(
                        "INSERT INTO api_display_policy(id, tool_cards_enabled, "
                        "skill_card_enabled, research_map_svg_enabled, "
                        "research_map_mermaid_enabled, research_map_html_enabled, "
                        "research_map_markdown_enabled, version, updated_by, updated_at) "
                        "SELECT id, tool_cards_enabled, skill_card_enabled, 1, 0, 0, 1, "
                        "version, updated_by, updated_at FROM api_display_policy_pre_v9"
                    )
                conn.execute("DROP TABLE api_display_policy_pre_v9")
            if "tool_error_cards_enabled" not in {
                str(column[1])
                for column in conn.execute("PRAGMA table_info(api_display_policy)").fetchall()
            }:
                # Error-status cards (the ⚠️ timeout/failure one-liners) leave
                # the formal output by default; administrators can re-enable
                # them. Additive column like the max_upload_bytes precedent —
                # existing databases inherit the suppressed default without
                # changing their optimistic-lock policy version.
                conn.execute(
                    "ALTER TABLE api_display_policy ADD COLUMN tool_error_cards_enabled "
                    "INTEGER NOT NULL DEFAULT 0 CHECK (tool_error_cards_enabled IN (0, 1))"
                )
            if "bibtex_export_mode" not in {
                str(column[1])
                for column in conn.execute("PRAGMA table_info(api_display_policy)").fetchall()
            }:
                # 清小搭 cannot download .bib attachments, so citation exports
                # default to a byte-identical .md copy the user renames back to
                # .bib. Additive column like the tool_error_cards_enabled
                # precedent — existing databases inherit the md_only default
                # without changing their optimistic-lock policy version.
                conn.execute(
                    "ALTER TABLE api_display_policy ADD COLUMN bibtex_export_mode "
                    "TEXT NOT NULL DEFAULT 'md_only' "
                    "CHECK (bibtex_export_mode IN ('bib_and_md', 'md_only', 'bib_only'))"
                )
            if previous_version < 4:
                # Legacy public-paper PDF retention is no longer a runtime
                # policy.  The retirement migration removes active artifacts;
                # keep only this harmless schema compatibility checkpoint.
                conn.execute(
                    "UPDATE api_storage_policy SET updated_by='migration-v4', "
                    "updated_at=?, version=version+1 WHERE id=1", (now,)
                )
            conn.execute(
                "INSERT INTO api_schema_meta(id, schema_version, applied_at) VALUES(1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET schema_version=excluded.schema_version, "
                "applied_at=CASE WHEN api_schema_meta.schema_version < excluded.schema_version "
                "THEN excluded.applied_at ELSE api_schema_meta.applied_at END",
                (SCHEMA_VERSION, now),
            )
            existing_policy = conn.execute(
                "SELECT version FROM api_storage_policy WHERE id = 1"
            ).fetchone()
            if existing_policy is None:
                preset_name = (os.getenv("OPENAI_API_POLICY_PRESET") or "balanced").strip().lower()
                try:
                    bootstrap_policy = POLICY_PRESETS[preset_name]
                except KeyError as exc:
                    raise ApiStorageError(
                        "OPENAI_API_POLICY_PRESET must be privacy, balanced or performance"
                    ) from exc
                values = bootstrap_policy | {
                    "version": 1, "updated_by": "bootstrap", "updated_at": now,
                }
                columns = list(values)
                conn.execute(
                    f"INSERT INTO api_storage_policy(id, {', '.join(columns)}) "
                    f"VALUES(1, {', '.join('?' for _ in columns)})",
                    [values[column] for column in columns],
                )
            conn.execute(
                "INSERT OR IGNORE INTO api_runtime_state"
                "(id, heavy_writes_paused, updated_at) VALUES(1, 0, ?)",
                (now,),
            )
            display_insert = conn.execute(
                "INSERT OR IGNORE INTO api_display_policy"
                "(id, tool_cards_enabled, tool_error_cards_enabled, "
                "skill_card_enabled, "
                "research_map_svg_enabled, research_map_mermaid_enabled, "
                "research_map_html_enabled, research_map_markdown_enabled, "
                "bibtex_export_mode, "
                "version, updated_by, updated_at) "
                "VALUES(1, 1, 0, 1, 1, 0, 0, 0, 'md_only', 1, 'bootstrap', ?)",
                (now,),
            )
            display_policy_changed = display_policy_changed or display_insert.rowcount == 1
            conn.commit()
        if display_policy_changed:
            _display_policy_cache.pop(str(self.db_path.resolve()), None)
        self._secure_db_files()

    def _secure_db_files(self) -> None:
        # The main database must always exist. SQLite WAL/SHM sidecars, however,
        # can disappear between ``Path.exists()`` and chmod when the last
        # connection closes and SQLite checkpoints/removes them. Treat that
        # narrow sidecar TOCTOU as normal, while still surfacing a present,
        # invalid sidecar (including a path/security violation).
        self.context.secure_private_file(self.db_path)
        for path in (Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")):
            if not os.path.lexists(path):
                continue
            try:
                self.context.secure_private_file(path)
            except (FileNotFoundError, StoragePathError):
                if os.path.lexists(path):
                    raise

    def _create_private_db_file(self) -> None:
        self.context.resolve_relative(self.db_path.relative_to(self.context.root_dir))
        fd = os.open(self.db_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self.db_path.exists():
            raise ApiStorageError("API state database is not initialized")
        self.context.resolve_relative(self.db_path.relative_to(self.context.root_dir))
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        self._secure_db_files()
        try:
            yield conn
        finally:
            conn.close()
            self._secure_db_files()

    def get_or_create_secret(self, name: str) -> bytes:
        """Get a server secret from API-only SQLite, creating it once."""
        if not name or len(name) > 80:
            raise ValueError("invalid secret name")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT secret_blob FROM api_secrets WHERE name = ?", (name,)).fetchone()
            if row is None:
                secret = secrets.token_bytes(32)
                conn.execute(
                    "INSERT INTO api_secrets(name, secret_blob, created_at) VALUES (?, ?, ?)",
                    (name, secret, time.time()),
                )
                conn.commit()
                return secret
            conn.commit()
            return bytes(row[0])

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT schema_version FROM api_schema_meta WHERE id = 1"
            ).fetchone()
        if row is None:
            raise ApiStorageError("API schema metadata is missing")
        return int(row[0])

    def get_policy(self) -> ApiStoragePolicy:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM api_storage_policy WHERE id = 1").fetchone()
        if row is None:
            raise ApiStorageError("API storage policy is missing")
        values = {field: row[field] for field in ApiStoragePolicy.__dataclass_fields__}
        return ApiStoragePolicy(**values)

    def update_policy(
        self, changes: Mapping[str, Any], *, expected_version: int, updated_by: str
    ) -> ApiStoragePolicy:
        unknown = set(changes) - _POLICY_COLUMNS
        if unknown:
            raise ValueError(f"unsupported policy fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_policy()
        actor = (updated_by or "").strip()
        if not actor or len(actor) > 128:
            raise ValueError("updated_by must be 1-128 characters")
        current = self.get_policy()
        merged = asdict(current)
        merged.update(changes)
        self._validate_policy(merged)
        assignments = [f"{column} = ?" for column in sorted(changes)]
        now = time.time()
        params = [changes[column] for column in sorted(changes)]
        params.extend([actor, now, expected_version])
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE api_storage_policy SET " + ", ".join(assignments) +
                ", updated_by = ?, updated_at = ?, version = version + 1 "
                "WHERE id = 1 AND version = ?",
                params,
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise PolicyVersionConflict(
                    f"storage policy version conflict (expected {expected_version})"
                )
            conn.commit()
        return self.get_policy()

    # ------------------------------------------------------------------
    # /v1 markdown-card display policy
    # ------------------------------------------------------------------

    _DISPLAY_COLUMNS = {
        "tool_cards_enabled": "tool_cards_enabled",
        "tool_error_cards_enabled": "tool_error_cards_enabled",
        "skill_card_enabled": "skill_card_enabled",
        "research_map_svg_enabled": "research_map_svg_enabled",
        "research_map_mermaid_enabled": "research_map_mermaid_enabled",
        "research_map_html_enabled": "research_map_html_enabled",
        "research_map_markdown_enabled": "research_map_markdown_enabled",
        "bibtex_export_mode": "bibtex_export_mode",
    }

    def get_display_policy(self) -> ApiDisplayPolicy:
        """Current display policy with a five-second single-worker cache."""
        cache_key = str(self.db_path.resolve())
        cached = _display_policy_cache.get(cache_key)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM api_display_policy WHERE id = 1").fetchone()
        if row is None:
            policy = ApiDisplayPolicy()
        else:
            policy = ApiDisplayPolicy(
                tool_cards_enabled=bool(row["tool_cards_enabled"]),
                tool_error_cards_enabled=bool(row["tool_error_cards_enabled"]),
                skill_card_enabled=bool(row["skill_card_enabled"]),
                research_map_svg_enabled=bool(row["research_map_svg_enabled"]),
                research_map_mermaid_enabled=bool(row["research_map_mermaid_enabled"]),
                research_map_html_enabled=bool(row["research_map_html_enabled"]),
                research_map_markdown_enabled=bool(row["research_map_markdown_enabled"]),
                bibtex_export_mode=str(row["bibtex_export_mode"]),
                version=int(row["version"]),
                updated_by=str(row["updated_by"]),
                updated_at=float(row["updated_at"]),
            )
        _display_policy_cache[cache_key] = (
            time.monotonic() + DISPLAY_POLICY_CACHE_TTL_SECONDS, policy
        )
        return policy

    def update_display_policy(
        self, changes: Mapping[str, Any], *, expected_version: int, updated_by: str
    ) -> ApiDisplayPolicy:
        """Optimistic-lock partial update over strict boolean display fields."""
        allowed = set(self._DISPLAY_COLUMNS)
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported display policy fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_display_policy()
        actor = (updated_by or "").strip()
        if not actor or len(actor) > 128:
            raise ValueError("updated_by must be 1-128 characters")
        current = self.get_display_policy()
        merged = {
            field: changes.get(field, getattr(current, field))
            for field in self._DISPLAY_COLUMNS
        }
        for field, value in merged.items():
            if field == "bibtex_export_mode":
                if value not in _BIBTEX_EXPORT_MODES:
                    raise ValueError(
                        "bibtex_export_mode must be one of: "
                        + ", ".join(sorted(_BIBTEX_EXPORT_MODES))
                    )
            elif not isinstance(value, bool):
                raise ValueError(f"{field} must be a boolean")
        if not any(merged[field] for field in (
            "research_map_svg_enabled", "research_map_mermaid_enabled",
            "research_map_html_enabled",
        )):
            raise ValueError("SVG、Mermaid、HTML 至少启用一种研究图谱输出")
        ordered = sorted(changes)
        assignments = [f"{self._DISPLAY_COLUMNS[column]} = ?" for column in ordered]
        now = time.time()
        params: list[Any] = [
            changes[column] if column == "bibtex_export_mode"
            else (1 if changes[column] else 0)
            for column in ordered
        ]
        params.extend([actor, now, expected_version])
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE api_display_policy SET " + ", ".join(assignments) +
                ", updated_by = ?, updated_at = ?, version = version + 1 "
                "WHERE id = 1 AND version = ?",
                params,
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise PolicyVersionConflict(
                    f"display policy version conflict (expected {expected_version})"
                )
            conn.commit()
        _display_policy_cache.pop(str(self.db_path.resolve()), None)
        return self.get_display_policy()

    @staticmethod
    def _validate_policy(policy: Mapping[str, Any]) -> None:
        for field, choices in _POLICY_ENUMS.items():
            if policy[field] not in choices:
                raise ValueError(f"invalid {field}")
        for field in _TTL_COLUMNS:
            value = policy[field]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        max_upload_bytes = policy["max_upload_bytes"]
        if (not isinstance(max_upload_bytes, int) or isinstance(max_upload_bytes, bool)
                or not MIN_API_UPLOAD_BYTES <= max_upload_bytes <= MAX_API_UPLOAD_BYTES):
            raise ValueError(
                "max_upload_bytes must be an integer between 1 MiB and 200 MiB"
            )
        interval = policy["cleanup_interval_minutes"]
        if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
            raise ValueError("cleanup_interval_minutes must be a positive integer")
        thresholds = [
            policy["observe_threshold_percent"], policy["pressure_threshold_percent"],
            policy["critical_threshold_percent"], policy["hard_stop_threshold_percent"],
        ]
        if any(not isinstance(v, int) or isinstance(v, bool) for v in thresholds):
            raise ValueError("disk thresholds must be integers")
        if not (0 < thresholds[0] < thresholds[1] < thresholds[2] < thresholds[3] <= 100):
            raise ValueError("disk thresholds must satisfy 0 < observe < pressure < critical < hard <= 100")
