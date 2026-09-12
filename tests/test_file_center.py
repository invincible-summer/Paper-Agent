"""File-center APIs: owned file listing, original download, paper aggregation,
and reader-open idempotency (one reading session per paper)."""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated uploads/ownership/history/reading stores + auth-off local user."""
    import core.auth_settings_store as auth_settings_store
    import core.history_store as history_store
    import core.reading_store as reading_store
    import core.web_artifact_store as web_artifact_store
    import tools.ingest.attachments as attachments

    monkeypatch.setattr(auth_settings_store, "_DB_PATH", tmp_path / "auth.db")
    auth_settings_store.reset_cache()
    current = auth_settings_store.get_auth_settings()
    if current.auth_required:
        auth_settings_store.update_auth_settings(
            {"auth_required": False},
            expected_version=current.version, updated_by="test")

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(attachments, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(web_artifact_store, "WEB_ARTIFACT_DB", tmp_path / "web_artifacts.db")
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    monkeypatch.setattr(history_store, "HISTORY_DIR", history_dir)
    monkeypatch.setattr(reading_store, "READING_DB", tmp_path / "reading.db")
    return {"uploads": uploads, "history": history_dir}


def _make_file(env, owner="local", ext="pdf", filename="论文 A.pdf", body=b"%PDF-1.4 fake"):
    import core.web_artifact_store as web_artifact_store

    aid = uuid.uuid4().hex
    (env["uploads"] / f"{aid}.{ext}").write_bytes(body)
    web_artifact_store.register_web_artifact("attachment", aid, owner, {
        "id": aid, "filename": filename, "ext": ext, "char_count": 100,
        "media_type": "application/pdf" if ext == "pdf" else "",
        "multimodal_status": "", "element_count": 0, "preview_url": "",
    })
    return aid


def _write_session(env, name, owner, papers=None, candidates=None, attachments=None):
    record = {
        "user_id": owner, "title": f"session {name}", "session_id": uuid.uuid4().hex[:16],
        "attachments": attachments or [], "messages": [],
        "papers": papers or [], "candidates": candidates or [],
    }
    (env["history"] / name).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


# --- GET /chat/files -------------------------------------------------------

def test_list_chat_files_returns_owned_attachments_with_size(env):
    from app.api.v1.chat import list_chat_files

    aid = _make_file(env, filename="综述.pdf", body=b"x" * 1234)
    other = _make_file(env, owner="someone-else", filename="别人的.pdf")

    result = list_chat_files("attachment")
    ids = [f["id"] for f in result["files"]]
    assert aid in ids and other not in ids
    mine = next(f for f in result["files"] if f["id"] == aid)
    assert mine["filename"] == "综述.pdf"
    assert mine["ext"] == "pdf"
    assert mine["size"] == 1234
    assert mine["created_at"]


def test_list_chat_files_rejects_unknown_kind(env):
    from fastapi import HTTPException
    from app.api.v1.chat import list_chat_files

    with pytest.raises(HTTPException) as exc:
        list_chat_files(kind="secret")
    assert exc.value.status_code == 400


# --- GET /chat/file/{id}/raw -------------------------------------------------

def test_raw_download_serves_document_as_attachment(env):
    from app.api.v1.chat import get_chat_file_raw

    aid = _make_file(env, filename="深度学习.pdf")
    resp = get_chat_file_raw(aid)
    assert resp.media_type == "application/pdf"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=utf-8''%E6%B7%B1%E5%BA%A6%E5%AD%A6%E4%B9%A0.pdf" in disposition


def test_raw_download_keeps_images_inline(env):
    from app.api.v1.chat import get_chat_file_raw

    aid = _make_file(env, ext="png", filename="图.png", body=b"\x89PNG")
    resp = get_chat_file_raw(aid)
    assert resp.headers["content-disposition"].startswith("inline;")


def test_raw_download_rejects_unowned_id(env):
    from fastapi import HTTPException
    from app.api.v1.chat import get_chat_file_raw

    aid = _make_file(env, owner="someone-else")
    with pytest.raises(HTTPException) as exc:
        get_chat_file_raw(aid)
    assert exc.value.status_code == 404


# --- GET /papers -------------------------------------------------------------

def test_collect_papers_dedups_across_sessions_and_isolates_owners(env):
    from app.api.v1.papers import collect_papers

    shared = {"id": "doi:10.1/x", "title": "Graph RAG Survey", "authors": ["A", "B"],
              "year": 2024, "doi": "10.1/x", "source": "crossref",
              "abstract": "abs " * 200, "urls": ["https://example.com"]}
    _write_session(env, "chat_1.json", "local", papers=[shared])
    _write_session(env, "chat_2.json", "local",
                   candidates=[{**shared, "id": "other-id"}, {"id": "p2", "title": "Second Paper"}])
    _write_session(env, "chat_3.json", "someone-else",
                   papers=[{"id": "p3", "title": "Not Mine"}])

    papers = collect_papers("local")
    assert [p["title"] for p in papers] == ["Graph RAG Survey", "Second Paper"]
    survey = papers[0]
    assert survey["in_review"] is True
    assert len(survey["sessions"]) == 2
    assert survey["abstract"].endswith("…")
    assert all(p["title"] != "Not Mine" for p in papers)


def test_papers_endpoint_returns_current_user_list(env):
    from app.api.v1.papers import list_papers

    _write_session(env, "chat_1.json", "local",
                   papers=[{"id": "p1", "title": "Only Paper", "urls": []}])
    assert list_papers() == {"papers": [{"id": "p1", "title": "Only Paper",
                                         "authors": [], "year": None, "venue": "", "doi": "",
                                         "source": "", "citation_count": None,
                                         "abstract": "", "keywords": [], "urls": [],
                                         "sessions": [{"filename": "chat_1.json", "title": "session chat_1.json"}],
                                         "in_review": True}]}


# --- POST /reader/open idempotency ------------------------------------------

def test_reader_open_reuses_existing_session_for_same_attachment(env):
    from app.api.v1.reader import open_document, OpenRequest

    aid = _make_file(env)
    _write_session(env, "chat_20260101_000000_reading.json", "local",
                   attachments=[{"id": aid, "filename": "论文 A.pdf", "ext": "pdf",
                                 "char_count": 1, "media_type": "application/pdf",
                                 "multimodal_status": "", "element_count": 0, "preview_url": ""}])

    first = asyncio.run(open_document(OpenRequest(attachment_id=aid), uid="local"))
    second = asyncio.run(open_document(OpenRequest(attachment_id=aid), uid="local"))
    assert first["session_id"] == second["session_id"]
    assert first["history_filename"] == second["history_filename"]


def test_reader_open_creates_draft_once_when_attachment_unknown(env):
    from app.api.v1.reader import open_document, OpenRequest

    aid = _make_file(env)
    first = asyncio.run(open_document(OpenRequest(attachment_id=aid), uid="local"))
    second = asyncio.run(open_document(OpenRequest(attachment_id=aid), uid="local"))
    assert first["session_id"] == second["session_id"]
