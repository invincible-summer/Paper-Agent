"""Module 4: API-only cleanup, pressure thresholds and confirmations."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from core.api_artifact_store import ApiArtifactStore
from core.api_storage_cleanup import ApiStorageCleanup
from core.api_storage_store import SCHEMA_VERSION, ApiStorageStore
from core.storage_context import StorageContext
from core.storage_pressure import StoragePressureError, StoragePressureGuard


def _storage(tmp_path: Path) -> ApiStorageStore:
    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path / "openai-api"))
    store.initialize()
    return store


def _session(store: ApiStorageStore, session_id: str, *, expires_at=None):
    now = time.time()
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO api_sessions(id, credential_id, rag_session_id, created_at, updated_at, last_accessed_at, expires_at) "
            "VALUES (?, 'key', ?, ?, ?, ?, ?)",
            (session_id, session_id, now, now, now, expires_at or now + 86400),
        )
        conn.commit()


def _artifact(store: ApiStorageStore, session_id: str, *, category="upload", expired=False):
    _session(store, session_id)
    source = store.context.temp_dir / f"{session_id}.bin"
    source.write_bytes(b"x" * 128)
    artifact_store = ApiArtifactStore(store)
    if category == "public_pdf":
        art = artifact_store.save_public_pdf(source, logical_name=f"{session_id}.pdf")
    elif category == "export":
        art = artifact_store.save_export(
            source, session_id=session_id, display_name=f"{session_id}.md", mime_type="text/markdown"
        )
    else:
        art = artifact_store.save_private_upload(
            source, session_id=session_id, logical_name=f"{session_id}.bin",
            mime_type="application/octet-stream", category=category,
        )
    source.unlink(missing_ok=True)
    with store.connect() as conn:
        conn.execute("UPDATE api_artifacts SET protected_until=0 WHERE id=?", (art.id,))
        if expired:
            conn.execute("UPDATE api_artifacts SET expires_at=0 WHERE id=?", (art.id,))
        conn.commit()
    return art


@pytest.mark.parametrize("percent,operation,allowed", [
    (74, "upload", True), (76, "upload", True), (86, "deep_read", True),
    (96, "upload", False), (96, "text_chat", True), (99, "export", False),
    (99, "text_chat", True),
])
def test_pressure_thresholds_pause_only_heavy(tmp_path: Path, percent, operation, allowed):
    store = _storage(tmp_path)
    guard = StoragePressureGuard(store.context, disk_percent=lambda: percent)
    if allowed:
        assert guard.ensure_allowed(operation)["allowed"] is True
    else:
        with pytest.raises(StoragePressureError):
            guard.ensure_allowed(operation)


def test_emergency_policy_allows_heavy_below_mandatory_98(tmp_path: Path):
    store = _storage(tmp_path)
    policy = store.get_policy()
    store.update_policy(
        {"critical_strategy": "emergency_evict"},
        expected_version=policy.version, updated_by="test",
    )
    assert StoragePressureGuard(store.context, disk_percent=lambda: 96).ensure_allowed("upload")["allowed"]
    with pytest.raises(StoragePressureError):
        StoragePressureGuard(store.context, disk_percent=lambda: 98).ensure_allowed("upload")


def test_pressure_auto_recovers_runtime_state(tmp_path: Path):
    store = _storage(tmp_path)
    values = iter([96, 70])
    guard = StoragePressureGuard(store.context, disk_percent=lambda: next(values))
    with pytest.raises(StoragePressureError):
        guard.ensure_allowed("download")
    assert guard.ensure_allowed("download")["allowed"]
    with store.connect() as conn:
        state = conn.execute("SELECT heavy_writes_paused, pause_reason FROM api_runtime_state").fetchone()
    assert state[0] == 0 and state[1] is None


def test_cleanup_expired_is_idempotent_and_never_touches_web(tmp_path: Path):
    store = _storage(tmp_path)
    art = _artifact(store, "expired-session", expired=True)
    web = tmp_path / "history_record" / "chat_keep.json"
    web.parent.mkdir(parents=True)
    web.write_text("keep", encoding="utf-8")
    cleanup = ApiStorageCleanup(store, disk_percent=lambda: 50)
    first = cleanup.run()
    second = cleanup.run()
    assert first.deleted_artifacts == 1
    assert second.deleted_artifacts == 0
    assert not art.path.exists()
    assert web.read_text(encoding="utf-8") == "keep"


def test_protected_and_inflight_artifacts_survive_cleanup(tmp_path: Path):
    store = _storage(tmp_path)
    protected = _artifact(store, "protected", expired=True)
    inflight = _artifact(store, "inflight", expired=True)
    now = time.time()
    with store.connect() as conn:
        conn.execute("UPDATE api_artifacts SET protected_until=? WHERE id=?", (now + 3600, protected.id))
        conn.execute(
            "INSERT INTO api_inflight_operations(id, session_id, operation, started_at, expires_at) VALUES ('op', 'inflight', 'upload', ?, ?)",
            (now, now + 3600),
        )
        conn.commit()
    ApiStorageCleanup(store, disk_percent=lambda: 50).run()
    assert protected.path.exists()
    assert inflight.path.exists()


def test_85_evicts_rebuildable_but_not_unexpired_private_upload(tmp_path: Path):
    store = _storage(tmp_path)
    private = _artifact(store, "private")
    public = _artifact(store, "public-owner", category="public_pdf")
    export = _artifact(store, "export-owner", category="export")
    result = ApiStorageCleanup(store, disk_percent=lambda: 86).run()
    assert private.path.exists()
    assert not public.path.exists()
    assert not export.path.exists()
    assert result.deleted_artifacts >= 2


def test_public_pdf_has_3_day_expiry_and_scheduled_cleanup_deletes_it(tmp_path: Path):
    store = _storage(tmp_path)
    public = _artifact(store, "public-owner", category="public_pdf")
    with store.connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM api_artifacts WHERE id=?", (public.id,)
        ).fetchone()
    assert row is not None and row[0] is not None
    with store.connect() as conn:
        created_at = conn.execute(
            "SELECT created_at FROM api_artifacts WHERE id=?", (public.id,)
        ).fetchone()[0]
    assert 0 < row[0] - created_at <= 3 * 24 * 3600 + 5

    # Expire the row manually (what scheduled cleanup does after 3 days) and
    # verify the file is removed; a later deep_read re-downloads + re-saves it.
    with store.connect() as conn:
        conn.execute("UPDATE api_artifacts SET expires_at=0 WHERE id=?", (public.id,))
        conn.commit()
    ApiStorageCleanup(store, disk_percent=lambda: 50).run()
    assert not public.path.exists()
    assert ApiArtifactStore(store).find_active(
        category="public_pdf", scope="public", logical_name="public-owner.pdf") is None


def test_expired_public_pdf_is_re_saved_with_fresh_expiry(tmp_path: Path):
    """After cleanup deletes an expired public PDF, the next deep_read download
    automatically re-registers it (the same SHA-256 blob) with a fresh TTL."""
    store = _storage(tmp_path)
    public = _artifact(store, "public-owner", category="public_pdf")
    with store.connect() as conn:
        conn.execute("UPDATE api_artifacts SET expires_at=0 WHERE id=?", (public.id,))
        conn.commit()
    assert ApiArtifactStore(store).find_active(
        category="public_pdf", scope="public", logical_name="public-owner.pdf") is None

    source = public.path
    re_saved = ApiArtifactStore(store).save_public_pdf(
        source, logical_name="public-owner.pdf")
    assert re_saved.id == public.id
    assert re_saved.path.exists()
    with store.connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM api_artifacts WHERE id=?", (public.id,)
        ).fetchone()
    assert row is not None and row[0] is not None and row[0] > time.time()
    assert ApiArtifactStore(store).find_active(
        category="public_pdf", scope="public", logical_name="public-owner.pdf") is not None


def test_v4_migration_shortens_existing_public_pdf_ttl_to_3_days(tmp_path: Path):
    context = StorageContext.openai_api(root_dir=tmp_path / "openai-api")
    store = ApiStorageStore(context)
    store.initialize()
    public = _artifact(store, "old-public", category="public_pdf")
    with store.connect() as conn:
        conn.execute("UPDATE api_artifacts SET expires_at=? WHERE id=?", (time.time() + 100000, public.id))
        conn.execute("UPDATE api_storage_policy SET public_pdf_ttl_seconds=2592000 WHERE id=1")
        conn.execute("UPDATE api_schema_meta SET schema_version=3 WHERE id=1")
        conn.commit()

    migrated = ApiStorageStore(context)
    migrated.initialize()
    assert migrated.schema_version() == SCHEMA_VERSION
    assert migrated.get_policy().public_pdf_ttl_seconds == 3 * 24 * 3600
    with migrated.connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM api_artifacts WHERE id=?", (public.id,)
        ).fetchone()
    # Existing rows are clamped to created_at + 3 days (not cleared).
    with migrated.connect() as conn:
        created_at = conn.execute(
            "SELECT created_at FROM api_artifacts WHERE id=?", (public.id,)
        ).fetchone()[0]
    assert row is not None and row[0] is not None
    assert row[0] <= created_at + 3 * 24 * 3600


def test_95_pause_policy_preserves_private_but_emergency_evicts_inactive(tmp_path: Path):
    pause_store = _storage(tmp_path / "pause")
    private_pause = _artifact(pause_store, "pause-private")
    with pause_store.connect() as conn:
        conn.execute("UPDATE api_artifacts SET last_accessed_at=0 WHERE id=?", (private_pause.id,))
        conn.commit()
    ApiStorageCleanup(pause_store, disk_percent=lambda: 96).run()
    assert private_pause.path.exists()

    emergency_store = _storage(tmp_path / "emergency")
    private_emergency = _artifact(emergency_store, "emergency-private")
    policy = emergency_store.get_policy()
    emergency_store.update_policy(
        {"critical_strategy": "emergency_evict"}, expected_version=policy.version,
        updated_by="test",
    )
    with emergency_store.connect() as conn:
        conn.execute("UPDATE api_artifacts SET last_accessed_at=0 WHERE id=?", (private_emergency.id,))
        conn.commit()
    ApiStorageCleanup(emergency_store, disk_percent=lambda: 96).run()
    assert not private_emergency.path.exists()
    with emergency_store.connect() as conn:
        assert conn.execute("SELECT status FROM api_artifacts WHERE id=?", (private_emergency.id,)).fetchone()[0] == "evicted"


def test_preview_is_non_destructive_single_use_and_version_bound(tmp_path: Path):
    store = _storage(tmp_path)
    art = _artifact(store, "preview", expired=True)
    cleanup = ApiStorageCleanup(store, disk_percent=lambda: 50)
    preview = cleanup.create_preview(action="immediate_cleanup", created_by="admin")
    assert art.path.exists()
    result = cleanup.execute_preview(preview["token"])
    assert result.deleted_artifacts == 1
    with pytest.raises(ValueError):
        cleanup.execute_preview(preview["token"])

    art2 = _artifact(store, "preview2", expired=True)
    preview2 = cleanup.create_preview(action="immediate_cleanup", created_by="admin")
    policy = store.get_policy()
    store.update_policy({"export_ttl_seconds": 7200}, expected_version=policy.version, updated_by="admin")
    with pytest.raises(ValueError, match="version"):
        cleanup.execute_preview(preview2["token"])
    assert art2.path.exists()


def test_reconcile_removes_only_old_api_orphan(tmp_path: Path):
    store = _storage(tmp_path)
    orphan = store.context.blob_dir / "orphan.bin"
    orphan.write_bytes(b"orphan")
    os.utime(orphan, (0, 0))
    web = tmp_path / "web" / "orphan.bin"
    web.parent.mkdir()
    web.write_bytes(b"web")
    ApiStorageCleanup(store).reconcile()
    assert not orphan.exists()
    assert web.exists()


@pytest.mark.anyio
async def test_inflight_context_registers_and_releases(tmp_path: Path):
    store = _storage(tmp_path)
    _session(store, "session")
    guard = StoragePressureGuard(store.context, disk_percent=lambda: 50)
    async with guard.protect("upload", "session"):
        with store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM api_inflight_operations").fetchone()[0] == 1
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_inflight_operations").fetchone()[0] == 0


@pytest.mark.anyio
async def test_execute_tool_returns_storage_pressure_error_without_calling_impl(monkeypatch, tmp_path: Path):
    import agents.tools_impl as tools_impl
    from agents.session import ChatSession
    from core.storage_pressure import StoragePressureError

    context = StorageContext.openai_api(root_dir=tmp_path / "openai-api")
    store = ApiStorageStore(context)
    store.initialize()
    _session(store, "tool-session")
    session = ChatSession(
        channel="openai_api", storage_context=context, session_id="tool-session"
    )
    called = False

    class FakeGuard:
        def protect(self, operation, session_id):
            class CM:
                async def __aenter__(self):
                    raise StoragePressureError(operation, 96, 95, "critical_pause_heavy")
                async def __aexit__(self, *args):
                    return False
            return CM()

    async def fake_impl(args, session, cb):
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr("core.storage_pressure.guard_for_session", lambda session: FakeGuard())
    monkeypatch.setitem(tools_impl._IMPLS, "deep_read", fake_impl)
    result = await tools_impl.execute_tool(
        {"name": "deep_read", "args": {"paper_ids": [], "attachment_ids": []}}, session
    )
    assert result.error_code == "STORAGE_PRESSURE"
    assert called is False
