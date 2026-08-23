"""Admin per-account storage census + unrecoverable deletion."""
from __future__ import annotations

import json
import time
from pathlib import Path

import core.admin_accounts as aa
import core.user_store as user_store
from core.api_storage_store import ApiStorageStore
from core.storage_context import StorageContext


def _history(root: Path, user_id: str, name: str, *, attachments=(), trace_ids=(), papers=()):
    data = {
        "type": "chat", "topic": name, "user_id": user_id,
        "messages": [{"role": "user", "content": name}],
        "attachments": [{"id": aid, "filename": f"{aid}.pdf"} for aid in attachments],
        "trace_ids": list(trace_ids),
        "papers": papers,
        "session_id": f"session-{name}",
    }
    fp = root / "history_record" / f"chat_{name}.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(data, ensure_ascii=False))
    return fp


def _file(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _users():
    return [
        {"id": "local", "username": "local", "display_name": "本地用户", "role": "local", "disabled": False},
        {"id": "user-a", "username": "a", "display_name": "用户A", "role": "user", "disabled": False},
        {"id": "user-b", "username": "b", "display_name": "用户B", "role": "user", "disabled": False},
    ]


def test_census_counts_histories_uploads_and_traces(tmp_path, monkeypatch):
    root = tmp_path / "project"
    aid = "a" * 32
    upload = _file(root / "data" / "uploads" / f"{aid}.pdf", b"%PDF-1.4 upload")
    sidecar = _file(root / "data" / "uploads" / f"{aid}.txt", b"extracted")
    trace = _file(root / "history_record" / "trace" / "trace_trace0001.jsonl", b'{"trace":1}')
    _history(root, "user-a", "a1", attachments=[aid], trace_ids=["trace0001"],
             papers=[{"id": "doi:1", "title": "T"}])

    monkeypatch.setattr(user_store, "list_users", _users)
    data = aa.list_accounts_data(project_root=root)
    items = {i["account_id"]: i for i in data["items"]}
    row = items["user-a"]
    assert row["history_count"] == 1
    assert row["history_bytes"] > 0
    assert row["attachment_count"] == 1
    assert row["upload_count"] == 2
    assert row["upload_bytes"] == upload.stat().st_size + sidecar.stat().st_size
    assert row["trace_count"] == 1
    assert row["trace_bytes"] == trace.stat().st_size
    assert items["local"]["account_type"] == "web_user"


def test_delete_web_account_removes_only_its_files(tmp_path, monkeypatch):
    root = tmp_path / "project"
    aid_a = "a" * 32
    aid_shared = "b" * 32
    upload_a = _file(root / "data" / "uploads" / f"{aid_a}.pdf", b"%PDF-1.4 a")
    shared = _file(root / "data" / "uploads" / f"{aid_shared}.pdf", b"%PDF-1.4 shared")
    trace_a = _file(root / "history_record" / "trace" / "trace_trace0001.jsonl", b'{"t":1}')
    _history(root, "user-a", "a1", attachments=[aid_a, aid_shared], trace_ids=["trace0001"])
    _history(root, "user-b", "b1", attachments=[aid_shared], trace_ids=["trace0001"])

    result = aa.delete_web_account_data("user-a", project_root=root)
    assert result["deleted"] is True
    assert result["histories"] == 1
    assert not upload_a.exists()
    assert shared.exists()  # referenced by user-b, must not be deleted
    assert trace_a.exists()  # also referenced by user-b, so it must be kept
    # History file is gone.
    assert not list((root / "history_record").glob("chat_a1.json"))

    # Re-run is a clean no-op (nothing left to delete).
    again = aa.delete_web_account_data("user-a", project_root=root)
    assert again["deleted"] is False
    assert again["reason"] == "account_not_found"


def test_delete_api_key_revokes_and_removes_private_files(tmp_path, monkeypatch):
    api_root = tmp_path / "api"
    context = StorageContext.openai_api(root_dir=api_root)
    store = ApiStorageStore(context)
    store.initialize()

    now = time.time()
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO api_sessions(id, credential_id, rag_session_id, created_at, updated_at, last_accessed_at, expires_at, checkpoint_uncompressed_bytes) "
            "VALUES ('sess-1', 'key-1', 'rag-1', ?, ?, ?, ?, 1024)",
            (now, now, now, now + 86400),
        )
        conn.commit()
    art_path = context.resolve_relative("blobs/upload/k/k1.pdf")
    art_path.parent.mkdir(parents=True, exist_ok=True)
    art_path.write_bytes(b"%PDF-1.4 private")
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO api_artifacts"
            "(id, content_hash, scope, category, privacy_class, logical_name, public_alias, mime_type, relative_path, size_bytes, created_at, last_accessed_at, expires_at, protected_until, status) "
            "VALUES ('art-1', 'hash1', 'sess-1', 'upload', 'private', 'k1.pdf', NULL, 'application/pdf', 'blobs/upload/k/k1.pdf', 14, ?, ?, ?, 0, 'active')",
            (now, now, now + 86400),
        )
        conn.execute(
            "INSERT INTO api_session_artifacts(session_id, artifact_id, relation, created_at) VALUES ('sess-1', 'art-1', 'upload', ?)",
            (now,),
        )
        conn.commit()

    monkeypatch.setattr(
        user_store, "list_agent_api_keys",
        lambda: [{"id": "key-1", "name": "k", "key_prefix": "pa_", "key_suffix": "x",
                  "created_at": now, "last_used_at": None, "revoked_at": None}],
    )
    revoked = {"value": False}
    monkeypatch.setattr(user_store, "revoke_agent_api_key",
                        lambda key_id: revoked.update(value=True) or True)

    result = aa.delete_api_key_data("key-1", api_root=api_root)
    assert result["deleted"] is True
    assert result["sessions"] == 1
    assert result["artifacts"] == 1
    assert result["revoked"] is True
    assert not art_path.exists()
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_sessions WHERE credential_id='key-1'").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM api_artifacts WHERE id='art-1'").fetchone()[0] == "deleted"


def test_secure_unlink_overwrites_then_removes(tmp_path):
    fp = _file(tmp_path / "secret.json", b"private-data-" * 100)
    ok, size = aa._secure_unlink(fp)
    assert ok is True
    assert size > 0
    assert not fp.exists()
