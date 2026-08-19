"""Module 1: isolated OpenAI API storage foundation."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from agents.session import ChatSession, save_chat_history
from core.api_storage_store import (
    ApiStoragePolicy,
    ApiStorageStore,
    PolicyVersionConflict,
    SCHEMA_VERSION,
)
from core.storage_context import StorageContext, StoragePathError


def test_api_context_is_rooted_and_does_not_create_runtime_data(tmp_path: Path):
    root = tmp_path / "api-root"
    context = StorageContext.openai_api(root_dir=root)
    assert context.channel == "openai_api"
    assert context.root_dir == root
    assert context.state_db == root / "state.db"
    assert context.metadata_db == root / "metadata.db"
    assert context.chroma_dir == root / "chroma"
    assert not root.exists()

    web = StorageContext.web(project_root=tmp_path / "project")
    assert web.channel == "web"
    assert web.root_dir == tmp_path / "project"
    assert web.chroma_dir == tmp_path / "project" / "data" / "chroma"
    assert web.metadata_db == tmp_path / "project" / "data" / "metadata.db"
    assert not web.root_dir.exists()


def test_context_rejects_bad_workspace_and_path_traversal(tmp_path: Path):
    with pytest.raises(StoragePathError):
        StorageContext.openai_api(workspace_id="../escape", root_dir=tmp_path / "api")
    with pytest.raises(StoragePathError):
        StorageContext.openai_api(workspace_id="with space", root_dir=tmp_path / "api")

    context = StorageContext.openai_api(root_dir=tmp_path / "api")
    with pytest.raises(StoragePathError):
        context.resolve_relative("../outside")
    with pytest.raises(StoragePathError):
        context.resolve_relative(Path("/tmp/outside"))


def test_context_rejects_symlink_escape(tmp_path: Path):
    root = tmp_path / "api"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "blobs"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(StoragePathError):
        StorageContext.openai_api(root_dir=root)


def test_api_layout_permissions_and_private_database(tmp_path: Path):
    context = StorageContext.openai_api(root_dir=tmp_path / "api")
    context.ensure_layout()
    assert context.root_dir.stat().st_mode & 0o777 == 0o700
    assert context.blob_dir.stat().st_mode & 0o777 == 0o700
    store = ApiStorageStore(context)
    store.initialize()
    assert context.state_db.stat().st_mode & 0o777 == 0o600
    assert store.schema_version() == SCHEMA_VERSION


def test_schema_is_idempotent_wal_and_foreign_keys(tmp_path: Path):
    context = StorageContext.openai_api(root_dir=tmp_path / "api")
    store = ApiStorageStore(context)
    store.initialize()
    first = context.state_db.stat().st_size
    store.initialize()
    assert store.schema_version() == SCHEMA_VERSION
    with store.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "api_schema_meta", "api_storage_policy", "api_sessions",
        "api_session_aliases", "api_artifacts", "api_session_artifacts",
        "api_cleanup_runs", "api_cleanup_previews", "api_runtime_state",
        "api_inflight_operations", "api_trace_aggregates", "api_trace_records",
        "api_secrets",
    } <= tables
    assert context.state_db.stat().st_size >= first


def test_wal_sidecar_disappearing_during_security_hardening_is_ignored(
    tmp_path: Path, monkeypatch
):
    context = StorageContext.openai_api(root_dir=tmp_path / "api")
    store = ApiStorageStore(context)
    store.initialize()
    wal_path = Path(f"{context.state_db}-wal")
    wal_path.write_bytes(b"sidecar")

    original = StorageContext.secure_private_file

    def disappear_once(self, path):
        if Path(path) == wal_path:
            wal_path.unlink()
            raise StoragePathError("private storage file does not exist")
        return original(self, path)

    monkeypatch.setattr(StorageContext, "secure_private_file", disappear_once)
    store._secure_db_files()


def test_present_invalid_wal_sidecar_is_not_ignored(tmp_path: Path):
    context = StorageContext.openai_api(root_dir=tmp_path / "api")
    store = ApiStorageStore(context)
    store.initialize()
    wal_path = Path(f"{context.state_db}-wal")
    wal_path.symlink_to(tmp_path / "missing-target")

    with pytest.raises(StoragePathError):
        store._secure_db_files()


def test_exact_balanced_default_policy(tmp_path: Path):
    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path / "api"))
    store.initialize()
    policy = store.get_policy()
    assert policy == ApiStoragePolicy(
        preset="balanced",
        session_ttl_seconds=604800,
        upload_ttl_seconds=604800,
        export_ttl_seconds=86400,
        public_pdf_ttl_seconds=259200,
        cache_ttl_seconds=7776000,
        trace_ttl_seconds=604800,
        cleanup_interval_minutes=60,
        observe_threshold_percent=75,
        pressure_threshold_percent=85,
        critical_threshold_percent=95,
        hard_stop_threshold_percent=98,
        pressure_strategy="continuous_evict",
        critical_strategy="pause_heavy",
        trace_mode="off",
        version=1,
        updated_by="bootstrap",
        updated_at=policy.updated_at,
    )
    assert policy.max_upload_bytes == 200 * 1024 * 1024
    assert policy.updated_at > 0


def test_schema_v5_migrates_max_upload_bytes_without_policy_version_bump(tmp_path: Path):
    context = StorageContext.openai_api(root_dir=tmp_path / "api")
    store = ApiStorageStore(context)
    store.initialize()
    before = store.get_policy()
    with store.connect() as conn:
        conn.execute("ALTER TABLE api_storage_policy RENAME TO api_storage_policy_v5")
        conn.execute("""
            CREATE TABLE api_storage_policy (
                id INTEGER PRIMARY KEY CHECK (id = 1), preset TEXT NOT NULL,
                session_ttl_seconds INTEGER NOT NULL, upload_ttl_seconds INTEGER NOT NULL,
                export_ttl_seconds INTEGER NOT NULL, public_pdf_ttl_seconds INTEGER NOT NULL,
                cache_ttl_seconds INTEGER NOT NULL, trace_ttl_seconds INTEGER NOT NULL,
                cleanup_interval_minutes INTEGER NOT NULL,
                observe_threshold_percent INTEGER NOT NULL,
                pressure_threshold_percent INTEGER NOT NULL,
                critical_threshold_percent INTEGER NOT NULL,
                hard_stop_threshold_percent INTEGER NOT NULL,
                pressure_strategy TEXT NOT NULL, critical_strategy TEXT NOT NULL,
                trace_mode TEXT NOT NULL, version INTEGER NOT NULL,
                updated_by TEXT NOT NULL, updated_at REAL NOT NULL
            )
        """)
        old_columns = [
            "id", "preset", "session_ttl_seconds", "upload_ttl_seconds",
            "export_ttl_seconds", "public_pdf_ttl_seconds", "cache_ttl_seconds",
            "trace_ttl_seconds", "cleanup_interval_minutes",
            "observe_threshold_percent", "pressure_threshold_percent",
            "critical_threshold_percent", "hard_stop_threshold_percent",
            "pressure_strategy", "critical_strategy", "trace_mode", "version",
            "updated_by", "updated_at",
        ]
        joined = ", ".join(old_columns)
        conn.execute(f"INSERT INTO api_storage_policy({joined}) SELECT {joined} FROM api_storage_policy_v5")
        conn.execute("DROP TABLE api_storage_policy_v5")
        conn.execute("UPDATE api_schema_meta SET schema_version = 5 WHERE id = 1")
        conn.commit()

    store.initialize()
    migrated = store.get_policy()
    assert store.schema_version() == SCHEMA_VERSION
    assert migrated.max_upload_bytes == 200 * 1024 * 1024
    assert migrated.version == before.version


def test_schema_v6_migrates_display_policy_preset_to_toggle(tmp_path: Path):
    context = StorageContext.openai_api(root_dir=tmp_path / "api")
    store = ApiStorageStore(context)
    store.initialize()

    def _rebuild_legacy(preset: str, skill: int, version: int) -> None:
        with store.connect() as conn:
            conn.execute("DROP TABLE api_display_policy")
            conn.execute("""
                CREATE TABLE api_display_policy (
                    id INTEGER PRIMARY KEY CHECK (id = 1), preset TEXT NOT NULL,
                    enabled_tools_json TEXT NOT NULL DEFAULT '[]',
                    skill_card_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (skill_card_enabled IN (0, 1)),
                    version INTEGER NOT NULL CHECK (version > 0),
                    updated_by TEXT NOT NULL, updated_at REAL NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO api_display_policy(id, preset, enabled_tools_json, "
                "skill_card_enabled, version, updated_by, updated_at) "
                "VALUES(1, ?, '[]', ?, ?, 'legacy-admin', 123.0)",
                (preset, skill, version),
            )
            conn.execute("UPDATE api_schema_meta SET schema_version = 6 WHERE id = 1")
            conn.commit()

    _rebuild_legacy("custom", 0, 4)
    store.initialize()
    policy = store.get_display_policy()
    assert store.schema_version() == SCHEMA_VERSION
    assert policy.tool_cards_enabled is True  # anything except 'off' maps to on
    assert policy.skill_card_enabled is False
    assert policy.version == 4 and policy.updated_by == "legacy-admin"

    _rebuild_legacy("off", 1, 2)
    store.initialize()
    policy = store.get_display_policy()
    assert policy.tool_cards_enabled is False
    assert policy.skill_card_enabled is True
    assert policy.version == 2


def test_policy_upload_limit_validation(tmp_path: Path):
    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path / "api"))
    store.initialize()
    current = store.get_policy()
    updated = store.update_policy(
        {"max_upload_bytes": 8 * 1024 * 1024},
        expected_version=current.version,
        updated_by="admin-test",
    )
    assert updated.max_upload_bytes == 8 * 1024 * 1024
    with pytest.raises(ValueError, match="1 MiB and 200 MiB"):
        store.update_policy(
            {"max_upload_bytes": 200 * 1024 * 1024 + 1},
            expected_version=updated.version,
            updated_by="admin-test",
        )


def test_optimistic_policy_version_conflict(tmp_path: Path):
    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path / "api"))
    store.initialize()
    before = store.get_policy()
    after = store.update_policy(
        {"export_ttl_seconds": 3600},
        expected_version=before.version,
        updated_by="admin-test",
    )
    assert after.export_ttl_seconds == 3600
    assert after.version == before.version + 1
    assert after.updated_by == "admin-test"
    with pytest.raises(PolicyVersionConflict):
        store.update_policy(
            {"export_ttl_seconds": 7200},
            expected_version=before.version,
            updated_by="stale-admin",
        )


def test_policy_cross_field_validation(tmp_path: Path):
    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path / "api"))
    store.initialize()
    policy = store.get_policy()
    with pytest.raises(ValueError):
        store.update_policy(
            {"hard_stop_threshold_percent": 80},
            expected_version=policy.version,
            updated_by="admin",
        )
    with pytest.raises(ValueError):
        store.update_policy(
            {"trace_mode": "raw_reasoning"},
            expected_version=policy.version,
            updated_by="admin",
        )


def test_api_storage_does_not_touch_web_history_or_paths(tmp_path: Path):
    project = tmp_path / "project"
    history = project / "history_record" / "chat_web.json"
    history.parent.mkdir(parents=True)
    history.write_text('{"messages": [{"role": "user", "content": "keep"}]}', encoding="utf-8")
    web = StorageContext.web(project_root=project)
    api = ApiStorageStore(StorageContext.openai_api(root_dir=project / "data" / "openai_api"))
    api.initialize()
    assert history.read_text(encoding="utf-8") == '{"messages": [{"role": "user", "content": "keep"}]}'
    assert web.metadata_db == project / "data" / "metadata.db"
    assert api.context.root_dir != web.root_dir
    assert not (project / "data" / "metadata.db").exists()


def test_chat_session_defaults_to_web_and_history_omits_storage_paths(tmp_path: Path, monkeypatch):
    import core.history_store as history_store

    session = ChatSession(
        storage_context=StorageContext.web(project_root=tmp_path / "private-project")
    )
    assert session.channel == "web"
    monkeypatch.setattr(history_store, "HISTORY_DIR", tmp_path / "history")
    filename = save_chat_history(session)
    payload = (history_store.HISTORY_DIR / filename).read_text(encoding="utf-8")
    assert "storage_context" not in payload
    assert str(tmp_path / "private-project") not in payload
    assert '"channel"' not in payload


def test_chat_session_rejects_channel_context_mismatch(tmp_path: Path):
    with pytest.raises(ValueError, match="must match"):
        ChatSession(
            channel="web",
            storage_context=StorageContext.openai_api(root_dir=tmp_path / "api"),
        )


def test_initialization_never_copies_environment_secrets(tmp_path: Path, monkeypatch):
    secret = "DO_NOT_PERSIST_SECRET_123456789"
    monkeypatch.setenv("AGENT_API_KEY", secret)
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path / "api"))
    store.initialize()
    assert secret.encode() not in store.context.state_db.read_bytes()


def test_bootstrap_preset_is_used_once_then_database_wins(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_POLICY_PRESET", "privacy")
    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path / "api"))
    store.initialize()
    privacy = store.get_policy()
    assert privacy.preset == "privacy"
    assert privacy.session_ttl_seconds == 7200
    monkeypatch.setenv("OPENAI_API_POLICY_PRESET", "invalid-after-bootstrap")
    store.initialize()
    assert store.get_policy() == privacy


def test_invalid_bootstrap_preset_fails_without_touching_web(tmp_path: Path, monkeypatch):
    web_marker = tmp_path / "history_record" / "chat_keep.json"
    web_marker.parent.mkdir(parents=True)
    web_marker.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_POLICY_PRESET", "not-a-policy")
    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path / "data" / "openai_api"))
    with pytest.raises(Exception, match="OPENAI_API_POLICY_PRESET"):
        store.initialize()
    assert web_marker.read_text(encoding="utf-8") == "keep"
