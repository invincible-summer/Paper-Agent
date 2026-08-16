"""Module 5: API Trace policy, redaction and web compatibility."""
from __future__ import annotations

import json
import time
from pathlib import Path

from core.api_storage_cleanup import ApiStorageCleanup
from core.api_storage_store import ApiStorageStore
from core.storage_context import StorageContext
from core.trace import Trace


def _store(tmp_path: Path) -> ApiStorageStore:
    store = ApiStorageStore(StorageContext.openai_api(root_dir=tmp_path / "openai-api"))
    store.initialize()
    return store


def _set_mode(store: ApiStorageStore, mode: str):
    policy = store.get_policy()
    return store.update_policy({"trace_mode": mode}, expected_version=policy.version, updated_by="test")


def test_api_trace_off_writes_only_anonymous_aggregate(tmp_path: Path):
    store = _store(tmp_path)
    trace = Trace(channel="openai_api", storage_context=store.context)
    trace.start(user_query="PRIVATE QUESTION", api_key="SECRET")
    trace.event("tool_call", tool="deep_read", args={"url": "https://x/p.pdf?token=secret"})
    trace.add_tokens({"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
    trace.finish(status="done", iterations=1, tool_calls=1)
    assert not store.context.trace_dir.exists() or list(store.context.trace_dir.iterdir()) == []
    with store.connect() as conn:
        aggregate = conn.execute("SELECT requests, errors, total_tokens FROM api_trace_aggregates").fetchone()
        records = conn.execute("SELECT COUNT(*) FROM api_trace_records").fetchone()[0]
    assert tuple(aggregate) == (1, 0, 5)
    assert records == 0


def test_metadata_trace_contains_no_user_text_args_filename_or_url(tmp_path: Path):
    store = _store(tmp_path)
    _set_mode(store, "metadata")
    trace = Trace(channel="openai_api", storage_context=store.context)
    trace.start(user_query="PRIVATE QUESTION", filename="secret.pdf")
    trace.event("tool_result", tool="deep_read", status="success",
                args={"url": "https://example/x?token=secret"})
    trace.finish(status="done", iterations=1, tool_calls=1)
    text = trace.path.read_text(encoding="utf-8")
    assert "PRIVATE QUESTION" not in text
    assert "secret.pdf" not in text
    assert "https://example" not in text
    assert '"tool": "deep_read"' in text
    assert '"status": "success"' in text


def test_full_trace_redacts_keys_bytes_fulltext_and_url_query(tmp_path: Path):
    store = _store(tmp_path)
    _set_mode(store, "full")
    trace = Trace(channel="openai_api", storage_context=store.context)
    trace.start(user_query="allowed diagnostic question")
    trace.event(
        "debug", authorization="Bearer TOPSECRET", api_key="KEYSECRET",
        payload=b"raw-bytes", full_text="COMPLETE PDF",
        url="https://example/path?signature=secret",
    )
    trace.finish(status="error", error_code="UPSTREAM")
    text = trace.path.read_text(encoding="utf-8")
    assert "allowed diagnostic question" in text
    for secret in ("TOPSECRET", "KEYSECRET", "raw-bytes", "COMPLETE PDF", "signature=secret"):
        assert secret not in text
    assert "[REDACTED]" in text
    assert "https://example/path?[REDACTED]" in text


def test_trace_policy_hot_switch_and_ttl_cap(tmp_path: Path):
    store = _store(tmp_path)
    off = Trace(channel="openai_api", storage_context=store.context)
    assert off.mode == "off"
    _set_mode(store, "metadata")
    metadata = Trace(channel="openai_api", storage_context=store.context)
    assert metadata.mode == "metadata"
    metadata.start(model="m")
    metadata.finish(status="done")
    with store.connect() as conn:
        created, expires = conn.execute(
            "SELECT created_at, expires_at FROM api_trace_records WHERE trace_id=?", (metadata.trace_id,)
        ).fetchone()
    assert 0 < expires - created <= 7 * 24 * 3600 + 1


def test_expired_api_trace_cleanup_never_touches_web_trace(tmp_path: Path):
    store = _store(tmp_path)
    _set_mode(store, "metadata")
    trace = Trace(channel="openai_api", storage_context=store.context)
    trace.start(model="m")
    trace.finish(status="done")
    web = tmp_path / "history_record" / "trace" / "trace_web.jsonl"
    web.parent.mkdir(parents=True)
    web.write_text("web", encoding="utf-8")
    with store.connect() as conn:
        conn.execute("UPDATE api_trace_records SET expires_at=0")
        conn.commit()
    ApiStorageCleanup(store, disk_percent=lambda: 50).run()
    assert not trace.path.exists()
    assert web.read_text(encoding="utf-8") == "web"


def test_web_trace_behavior_remains_raw_and_in_original_directory(tmp_path: Path):
    trace = Trace(trace_dir=tmp_path / "web-traces")
    trace.start(user_query="web raw question")
    trace.finish(status="done")
    assert trace.path.is_file()
    assert "web raw question" in trace.path.read_text(encoding="utf-8")
