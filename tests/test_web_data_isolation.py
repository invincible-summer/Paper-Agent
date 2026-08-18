"""Web-channel ownership boundaries for uploads, exports and private assets."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


@pytest.fixture
def isolated_web_storage(tmp_path, monkeypatch):
    import core.web_artifact_store as ownership
    import tools.ingest.attachments as attachments

    monkeypatch.setattr(ownership, "WEB_ARTIFACT_DB", tmp_path / "web_artifacts.db")
    monkeypatch.setattr(attachments, "UPLOAD_DIR", tmp_path / "uploads")
    return tmp_path


def test_attachment_owner_is_required_for_raw_text_and_turn_scope(isolated_web_storage):
    from backend.app.api.v1 import chat as chat_api
    from core.web_artifact_store import owned_web_attachment
    from tools.ingest.attachments import save_attachment

    aid = "a" * 32
    record = save_attachment(
        b"private note", "private.txt", attachment_id=aid, owner_id="user-a",
    )
    assert record["id"] == aid
    assert owned_web_attachment(aid, "user-a") is not None
    assert owned_web_attachment(aid, "user-b") is None

    canonical = chat_api._owned_web_attachments(
        [{"id": aid, "filename": "tampered.txt", "ext": "pdf"}], "user-a"
    )
    assert canonical[0]["filename"] == "private.txt"
    assert canonical[0]["ext"] == "txt"
    with pytest.raises(Exception) as exc:
        chat_api._owned_web_attachments([{"id": aid}], "user-b")
    assert getattr(exc.value, "status_code", None) == 404


def test_web_file_routes_do_not_cross_user_boundaries(isolated_web_storage, monkeypatch):
    from fastapi import HTTPException
    from starlette.responses import FileResponse

    import app.api.v1.auth as auth_api
    import app.api.v1.chat as chat_api
    import app.api.v1.files as files_api
    import core.auth_settings_store as auth_settings_store
    from core.web_artifact_store import register_web_artifact
    from tools.ingest.attachments import save_attachment

    monkeypatch.setattr(auth_settings_store, "_DB_PATH", isolated_web_storage / "auth.db")
    auth_settings_store.reset_cache()
    current_flags = auth_settings_store.get_auth_settings()
    auth_settings_store.update_auth_settings(
        {"auth_required": True},
        expected_version=current_flags.version, updated_by="test")
    monkeypatch.setattr(auth_api, "current_user", lambda *_args: {"id": "user-a"})
    monkeypatch.setattr(chat_api, "current_user", lambda *_args: {"id": "user-a"})
    aid = "b" * 32
    save_attachment(b"secret", "secret.png", attachment_id=aid, owner_id="user-a")
    raw = chat_api.get_chat_file_raw(aid, None, None)
    assert isinstance(raw, FileResponse)

    monkeypatch.setattr(auth_api, "current_user", lambda *_args: {"id": "user-b"})
    monkeypatch.setattr(chat_api, "current_user", lambda *_args: {"id": "user-b"})
    with pytest.raises(HTTPException) as raw_error:
        chat_api.get_chat_file_raw(aid, None, None)
    assert raw_error.value.status_code == 404

    export = isolated_web_storage / "exports"
    export.mkdir()
    files_api._EXPORT_DIR = export
    name = "private-report.md"
    (export / name).write_text("private", encoding="utf-8")
    register_web_artifact("export", name, "user-a")

    monkeypatch.setattr(auth_api, "current_user", lambda *_args: {"id": "user-a"})
    assert isinstance(files_api.get_file(name), FileResponse)
    monkeypatch.setattr(auth_api, "current_user", lambda *_args: {"id": "user-b"})
    with pytest.raises(HTTPException) as export_error:
        files_api.get_file(name)
    assert export_error.value.status_code == 404
