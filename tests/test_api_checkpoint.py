"""Module 2: API identity, structured Checkpoint and restart recovery."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agents.session import ChatSession
from core.api_checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    EXTERNAL_FIELD_BYTES,
    ApiCheckpointStore,
    ApiCredentialPrincipal,
    build_checkpoint_state,
    canonical_message_chain,
    get_api_checkpoint_store,
    principal_from_token,
)
from core.api_storage_store import ApiStorageStore
from core.models import Paper, PaperSummary
from core.storage_context import StorageContext


def _store(tmp_path: Path) -> ApiCheckpointStore:
    context = StorageContext.openai_api(root_dir=tmp_path / "openai-api")
    return ApiCheckpointStore(ApiStorageStore(context))


def _principal(name: str = "key-1") -> ApiCredentialPrincipal:
    return ApiCredentialPrincipal(
        credential_id=name, key_id=name, created_by="administrator", source="database"
    )


def test_canonical_chain_excludes_only_current_user_and_complete_alias_differs():
    messages = [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "当前问题"},
    ]
    prior = canonical_message_chain(messages)
    complete = canonical_message_chain(messages, exclude_last_user=False)
    assert "当前问题" not in prior
    assert "第一问" in prior and "第一答" in prior
    assert "当前问题" in complete
    assert prior != complete
    # Content-array normalization is stable for equivalent line endings.
    assert canonical_message_chain([{"role": "user", "content": "a\r\nb"}]) == canonical_message_chain(
        [{"role": "user", "content": "a\nb"}]
    )


def test_principal_is_one_way_and_contains_no_raw_token():
    token = "DO_NOT_PERSIST_TOKEN_123"
    principal = principal_from_token(token)
    assert token not in repr(principal)
    assert principal.credential_id.startswith("development:")
    assert principal.credential_id == principal_from_token(token).credential_id


def test_first_turn_same_text_creates_separate_sessions_and_alias_collision_is_ambiguous(tmp_path: Path):
    store = _store(tmp_path)
    principal = _principal()
    first = [{"role": "user", "content": "相同开场"}]
    a = store.get_or_create(principal, "u", first)
    b = store.get_or_create(principal, "u", first)
    assert a.created and b.created and a.session_id != b.session_id
    for item, answer in ((a, "相同答复"), (b, "相同答复")):
        store.save_checkpoint_sync(item.session)
        store.add_alias_sync(item.session_id, principal, "u", first + [{"role": "assistant", "content": answer}])
    with store.storage.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*), MIN(ambiguous), MAX(ambiguous) FROM api_session_aliases"
        ).fetchone()
    assert row[0] == 2 and row[1] == 1 and row[2] == 1
    next_turn = first + [{"role": "assistant", "content": "答一"}, {"role": "user", "content": "继续"}]
    fresh = store.get_or_create(principal, "u", next_turn)
    assert fresh.created
    assert fresh.session_id not in {a.session_id, b.session_id}


def test_checkpoint_allowlist_excludes_messages_reasoning_fulltext_and_absolute_paths(tmp_path: Path):
    store = _store(tmp_path)
    session = store.get_or_create(_principal(), None, [{"role": "user", "content": "new"}]).session
    session.topic = "研究主题"
    session.messages = [{"role": "user", "content": "PRIVATE MESSAGE"}]
    session.papers = [Paper(id="p1", title="论文", pdf_path="/secret/paper.pdf", llm_reasoning="hidden")]
    session.paper_summaries = {
        "p1": PaperSummary(paper_id="p1", full_text="COMPLETE PDF TEXT", key_findings=["发现"])
    }
    session.attachments = [{"id": "upload-1", "filename": "a.pdf", "text_preview": "raw text", "char_count": 4}]
    session.loaded_skills = {"deep_read"}
    state = build_checkpoint_state(session)
    encoded = json.dumps(state, ensure_ascii=False)
    assert "PRIVATE MESSAGE" not in encoded
    assert "COMPLETE PDF TEXT" not in encoded
    assert "hidden" not in encoded
    assert "/secret/paper.pdf" not in encoded
    assert "raw text" not in encoded
    assert set(state) >= {"schema_version", "papers", "paper_summaries", "loaded_skills", "rag_session_id"}


def test_checkpoint_restart_restores_structured_state_but_not_messages(tmp_path: Path):
    store = _store(tmp_path)
    principal = _principal()
    load = store.get_or_create(principal, "caller", [{"role": "user", "content": "start"}])
    session = load.session
    session.topic = "唐吉诃德"
    session.papers = [Paper(
        id="p1", title="论文 1", abstract_source="openaire",
        abstract_policy_status="disabled", abstract_policy_reason="network",
        pdf_source="arxiv",
    )]
    session.paper_summaries = {"p1": PaperSummary(paper_id="p1", key_findings=["k"])}
    session.map_data = {"clusters": [{"label": "理论"}]}
    session.loaded_skills = {"skill_a", "skill_b"}
    session.messages = [{"role": "user", "content": "must not persist"}]
    store.save_checkpoint_sync(session)

    restarted = ApiCheckpointStore(ApiStorageStore(store.context))
    next_messages = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "continue"},
    ]
    # No alias yet: this is a new session. Establish the completed-turn alias.
    store.add_alias_sync(session.session_id, principal, "caller", [
        {"role": "user", "content": "start"}, {"role": "assistant", "content": "answer"}
    ])
    restored = restarted.get_or_create(principal, "caller", next_messages)
    assert not restored.created
    assert restored.session.topic == "唐吉诃德"
    assert restored.session.papers[0].id == "p1"
    assert restored.session.papers[0].abstract_source == "openaire"
    assert restored.session.papers[0].abstract_policy_status == "disabled"
    assert restored.session.papers[0].abstract_policy_reason == "network"
    assert restored.session.papers[0].pdf_source == ""
    assert restored.session.map_data["clusters"][0]["label"] == "理论"
    assert restored.session.loaded_skills == {"skill_a", "skill_b"}
    assert restored.session.messages == []


def test_large_checkpoint_field_is_externalized_and_rehydrated(tmp_path: Path):
    store = _store(tmp_path)
    principal = _principal()
    session = store.get_or_create(principal, None, [{"role": "user", "content": "x"}]).session
    session.literature_review = "综述" * (EXTERNAL_FIELD_BYTES // 2)
    store.save_checkpoint_sync(session)
    with store.storage.connect() as conn:
        artifact = conn.execute(
            "SELECT category, privacy_class, relative_path FROM api_artifacts"
        ).fetchone()
        blob = conn.execute("SELECT checkpoint_blob FROM api_sessions WHERE id = ?", (session.session_id,)).fetchone()[0]
    assert artifact[0] == "state_payload" and artifact[1] == "private"
    assert artifact[2].startswith("blobs/state_payload/")
    assert len(blob) < len(session.literature_review.encode("utf-8"))
    restored = store._load_checkpoint_sync(session.session_id)
    assert restored is not None
    assert restored.literature_review == session.literature_review


def test_corrupt_checkpoint_is_quarantined_and_does_not_guess(tmp_path: Path):
    store = _store(tmp_path)
    session = store.get_or_create(_principal(), None, [{"role": "user", "content": "x"}]).session
    store.save_checkpoint_sync(session)
    with store.storage.connect() as conn:
        conn.execute("UPDATE api_sessions SET checkpoint_blob = ? WHERE id = ?", (b"bad", session.session_id))
        conn.commit()
    assert store._load_checkpoint_sync(session.session_id) is None
    with store.storage.connect() as conn:
        assert conn.execute("SELECT status FROM api_sessions WHERE id = ?", (session.session_id,)).fetchone()[0] == "corrupt"


def test_expired_alias_and_session_are_not_restored(tmp_path: Path):
    store = _store(tmp_path)
    principal = _principal()
    initial = [{"role": "user", "content": "x"}]
    session = store.get_or_create(principal, None, initial).session
    session.topic = "expired"
    store.save_checkpoint_sync(session)
    completed = initial + [{"role": "assistant", "content": "done"}]
    store.add_alias_sync(session.session_id, principal, None, completed)
    with store.storage.connect() as conn:
        conn.execute("UPDATE api_session_aliases SET expires_at = 0")
        conn.execute("UPDATE api_sessions SET expires_at = 0")
        conn.commit()
    restored = store.get_or_create(principal, None, completed + [{"role": "user", "content": "next"}])
    assert restored.created


def test_final_alias_is_not_created_for_interrupted_turn(tmp_path: Path):
    store = _store(tmp_path)
    principal = _principal()
    initial = [{"role": "user", "content": "x"}]
    session = store.get_or_create(principal, None, initial).session
    store.save_checkpoint_sync(session)
    with store.storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_session_aliases").fetchone()[0] == 0
    # No finalize/add_alias call represents an interrupted/error stream.
    next_session = store.get_or_create(principal, None, initial + [{"role": "user", "content": "next"}])
    assert next_session.created


def test_checkpoint_database_has_no_message_or_reasoning_secret(tmp_path: Path):
    store = _store(tmp_path)
    session = store.get_or_create(_principal(), None, [{"role": "user", "content": "x"}]).session
    session.topic = "safe"
    session.messages = [{"role": "user", "content": "MESSAGE_SECRET"}]
    store.save_checkpoint_sync(session)
    with store.storage.connect() as conn:
        raw = b"".join(bytes(row[0] or b"") for row in conn.execute("SELECT checkpoint_blob FROM api_sessions"))
    assert b"MESSAGE_SECRET" not in raw
    assert b"reasoning_content" not in raw


def test_hmac_secret_is_created_once_and_isolated_per_storage_root(tmp_path: Path):
    first = _store(tmp_path / "one")
    second = ApiCheckpointStore(ApiStorageStore(first.context))
    assert first._secret() == second._secret()
    with first.storage.connect() as conn:
        row = conn.execute("SELECT secret_blob FROM api_secrets WHERE name = 'checkpoint_hmac'").fetchone()
    assert row is not None and len(bytes(row[0])) == 32
    other = _store(tmp_path / "two")
    assert first._secret() != other._secret()


def test_credential_and_openai_user_are_session_bound(tmp_path: Path):
    store = _store(tmp_path)
    messages = [{"role": "user", "content": "same"}]
    a = store.get_or_create(_principal("key-a"), "user-a", messages)
    b = store.get_or_create(_principal("key-b"), "user-a", messages)
    c = store.get_or_create(_principal("key-a"), "user-b", messages)
    assert len({a.session_id, b.session_id, c.session_id}) == 3


def test_checkpoint_rejects_over_8_mib_uncompressed_limit(tmp_path: Path):
    store = _store(tmp_path)
    session = store.get_or_create(_principal(), None, [{"role": "user", "content": "x"}]).session
    # Many independent small fields avoid the per-field externalization path,
    # while the final checkpoint still has a hard uncompressed ceiling.
    session.map_data = {str(i): "x" * 4096 for i in range(2500)}
    with pytest.raises(ValueError, match="8 MiB"):
        store.save_checkpoint_sync(session)


def test_stale_store_writer_gets_optimistic_conflict(tmp_path: Path):
    first = _store(tmp_path)
    session = first.get_or_create(_principal(), None, [{"role": "user", "content": "x"}]).session
    first.save_checkpoint_sync(session)
    second = ApiCheckpointStore(ApiStorageStore(first.context))
    loaded = second._load_checkpoint_sync(session.session_id)
    assert loaded is not None
    session.topic = "first writer"
    loaded.topic = "stale writer"
    first.save_checkpoint_sync(session)
    from core.api_checkpoint import CheckpointVersionConflict
    with pytest.raises(CheckpointVersionConflict):
        second.save_checkpoint_sync(loaded)


def test_api_network_pdf_fields_are_omitted_from_checkpoint(tmp_path: Path):
    store = _store(tmp_path)
    session = store.get_or_create(_principal(), None, [{"role": "user", "content": "x"}]).session
    session.papers = [Paper(
        id="p", title="P", pdf_path="/legacy/paper.pdf",
        pdf_url="https://example.org/p.pdf", pdf_source="arxiv",
        fulltext_status="available",
    )]
    store.save_checkpoint_sync(session)
    with store.storage.connect() as conn:
        blob = conn.execute("SELECT checkpoint_blob FROM api_sessions WHERE id = ?", (session.session_id,)).fetchone()[0]
    import zlib
    decoded = zlib.decompress(blob).decode("utf-8")
    assert str(store.context.root_dir) not in decoded
    assert "pdf_path" not in decoded
    assert "pdf_url" not in decoded
    assert "pdf_source" not in decoded
    assert "fulltext_status" not in decoded
    restored = store._load_checkpoint_sync(session.session_id)
    assert restored is not None
    assert restored.papers[0].pdf_path is None
    assert restored.papers[0].pdf_url is None


def test_stable_session_id_is_immediate_hmac_primary_and_credential_isolated(tmp_path: Path):
    store = _store(tmp_path)
    raw_session_id = "qing-session-raw-must-not-persist"
    first = store.get_or_create(
        _principal("key-a"), "caller", [{"role": "user", "content": "第一问"}],
        provider_session_id=raw_session_id,
    )
    assert first.created
    first.session.topic = "部分状态"
    first.session.papers = [Paper(id="p1", title="已恢复论文")]
    store.save_checkpoint_sync(first.session)

    # A completely different message chain still resolves through sessionId.
    resumed = store.get_or_create(
        _principal("key-a"), "another-user-field",
        [{"role": "user", "content": "可以"}],
        provider_session_id=raw_session_id,
    )
    assert not resumed.created
    assert resumed.session_id == first.session_id
    assert resumed.session.topic == "部分状态"
    assert resumed.session.papers[0].id == "p1"

    isolated = store.get_or_create(
        _principal("key-b"), "caller", [{"role": "user", "content": "可以"}],
        provider_session_id=raw_session_id,
    )
    assert isolated.created
    assert isolated.session_id != first.session_id
    assert raw_session_id.encode() not in (store.context.state_db).read_bytes()
