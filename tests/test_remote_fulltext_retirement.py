"""Selective/idempotent retirement of legacy network-paper derivatives."""
from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

from core.remote_fulltext_retirement import retire_remote_fulltext


def test_retirement_is_dry_run_selective_and_idempotent(tmp_path: Path):
    root = tmp_path / "project"
    (root / "data" / "pdfs").mkdir(parents=True)
    (root / "data" / "uploads").mkdir(parents=True)
    (root / "data" / "assets" / "network").mkdir(parents=True)
    (root / "data" / "assets" / "upload_a1").mkdir(parents=True)
    (root / "history_record").mkdir(parents=True)
    (root / "data" / "pdfs" / "remote.pdf").write_bytes(b"%PDF")
    (root / "data" / "uploads" / "a1.pdf").write_bytes(b"upload")
    (root / "data" / "assets" / "network" / "f.png").write_bytes(b"network")
    (root / "data" / "assets" / "upload_a1" / "f.png").write_bytes(b"upload-element")
    history = {
        "messages": [
            {"role": "user", "content": "保留"},
            {"role": "assistant", "content": "历史回答保留", "toolCalls": [{
                "result": {"data": {"papers": [
                    {"id": "n1", "title": "网络论文", "pdf_path": "data/pdfs/remote.pdf",
                     "pdf_url": "https://example.org/a.pdf", "fulltext_status": "available"},
                    {"id": "a1", "source": "upload", "title": "上传论文",
                     "pdf_path": "data/uploads/a1.pdf", "fulltext_status": "available"},
                ], "fulltext_core_available": 1}}
            }]},
        ],
        "papers": [{"id": "n1", "title": "网络论文", "pdf_path": "data/pdfs/remote.pdf",
                     "pdf_url": "https://example.org/a.pdf", "fulltext_status": "available"}],
        "attachments": [{"id": "a1", "filename": "a1.pdf"}],
        "paper_summaries": {
            "n1": {"paper_id": "n1", "full_text": "旧全文", "document_info": {"read_level": "full"}},
            "upload:a1": {"paper_id": "upload:a1", "full_text": "上传全文"},
        },
    }
    history_path = root / "history_record" / "chat.json"
    history_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    db_path = root / "data" / "metadata.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
        CREATE TABLE papers(id TEXT PRIMARY KEY, source TEXT, pdf_path TEXT, pdf_url TEXT, pdf_source TEXT);
        CREATE TABLE summary_cache(paper_id TEXT, field_profile TEXT, read_mode TEXT, summary_json TEXT);
        CREATE TABLE fulltext_status(paper_id TEXT);
        CREATE TABLE paper_elements(element_id TEXT, paper_id TEXT, asset_path TEXT);
        """)
        conn.execute("INSERT INTO papers VALUES ('n1','openalex','data/pdfs/remote.pdf','u','s')")
        conn.execute("INSERT INTO summary_cache VALUES ('n1','general','full','{}')")
        conn.execute("INSERT INTO fulltext_status VALUES ('n1')")
        conn.execute("INSERT INTO paper_elements VALUES ('n-element','n1','data/assets/network/f.png')")
        conn.commit()
    dry = retire_remote_fulltext(root, dry_run=True)
    assert dry["status"] == "dry_run"
    assert (root / "data" / "pdfs" / "remote.pdf").exists()
    applied = retire_remote_fulltext(root, dry_run=False)
    assert applied["status"] == "applied"
    assert not (root / "data" / "pdfs" / "remote.pdf").exists()
    assert (root / "data" / "uploads" / "a1.pdf").exists()
    assert (root / "data" / "assets" / "upload_a1" / "f.png").exists()
    cleaned = json.loads(history_path.read_text(encoding="utf-8"))
    assert cleaned["messages"][0]["content"] == "保留"
    nested = cleaned["messages"][1]["toolCalls"][0]["result"]["data"]
    assert "fulltext_core_available" not in nested
    assert "pdf_path" not in nested["papers"][0]
    assert "pdf_url" not in nested["papers"][0]
    assert nested["papers"][1]["pdf_path"] == "data/uploads/a1.pdf"
    assert "fulltext_status" not in nested["papers"][1]
    assert "pdf_path" not in cleaned["papers"][0]
    assert "n1" not in cleaned["paper_summaries"]
    assert "upload:a1" in cleaned["paper_summaries"]
    assert retire_remote_fulltext(root, dry_run=False)["status"] == "already_applied"


def test_retirement_keeps_abstract_cache_but_removes_only_full_mode(tmp_path: Path):
    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    db_path = root / "data" / "metadata.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
        CREATE TABLE papers(id TEXT PRIMARY KEY, source TEXT, pdf_path TEXT, pdf_url TEXT, pdf_source TEXT);
        CREATE TABLE summary_cache(paper_id TEXT, field_profile TEXT, read_mode TEXT, summary_json TEXT);
        CREATE TABLE fulltext_status(paper_id TEXT);
        CREATE TABLE paper_elements(element_id TEXT, paper_id TEXT, asset_path TEXT);
        """)
        conn.execute("INSERT INTO papers VALUES ('p','openalex',NULL,NULL,NULL)")
        conn.execute("INSERT INTO summary_cache VALUES ('p','general','abstract','{\"research_problem\":\"keep\"}')")
        conn.execute("INSERT INTO summary_cache VALUES ('p','general','full','{\"full_text\":\"remove\"}')")
        conn.execute("INSERT INTO fulltext_status VALUES ('p')")
        conn.commit()
    result = retire_remote_fulltext(root, dry_run=False)
    assert result["status"] == "applied"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT read_mode FROM summary_cache").fetchall()
        assert rows == [("abstract",)]
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='fulltext_status'"
        ).fetchone()[0] == 0


def test_externalized_summary_payload_is_scrubbed_by_field_name(tmp_path: Path):
    root = tmp_path / "project"
    api = root / "data" / "openai_api"
    (api / "blobs").mkdir(parents=True)
    (api / "state.db").parent.mkdir(parents=True, exist_ok=True)
    payload = zlib.compress(json.dumps({
        "n": {"paper_id": "n", "full_text": "remove"},
        "upload:a": {"paper_id": "upload:a", "full_text": "keep"},
    }, ensure_ascii=False).encode("utf-8"))
    payload_path = api / "blobs" / "summary.json.zlib"
    payload_path.write_bytes(payload)
    with sqlite3.connect(api / "state.db") as conn:
        conn.executescript("""
        CREATE TABLE api_sessions(
            id TEXT PRIMARY KEY, checkpoint_blob BLOB,
            checkpoint_uncompressed_bytes INTEGER);
        CREATE TABLE api_artifacts(
            id TEXT PRIMARY KEY, relative_path TEXT, category TEXT, status TEXT,
            logical_name TEXT, content_hash TEXT, size_bytes INTEGER,
            scope TEXT, public_alias TEXT);
        """)
        conn.execute(
            "INSERT INTO api_artifacts VALUES ('x','blobs/summary.json.zlib','state_payload','active','paper_summaries','',?,'session',NULL)",
            (len(payload),),
        )
        conn.commit()
    retire_remote_fulltext(root, api_roots=[api], dry_run=False)
    cleaned = json.loads(zlib.decompress(payload_path.read_bytes()).decode("utf-8"))
    assert "n" not in cleaned
    assert "upload:a" in cleaned

def test_retirement_clears_legacy_pdf_columns_when_pdf_source_is_absent(tmp_path: Path):
    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    db_path = root / "data" / "metadata.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
        CREATE TABLE papers(id TEXT PRIMARY KEY, source TEXT, pdf_path TEXT, pdf_url TEXT);
        CREATE TABLE summary_cache(paper_id TEXT, field_profile TEXT, read_mode TEXT, summary_json TEXT);
        CREATE TABLE paper_elements(element_id TEXT, paper_id TEXT, asset_path TEXT);
        """)
        conn.execute("INSERT INTO papers VALUES ('network','openalex','data/pdfs/network.pdf','https://example.org/network.pdf')")
        conn.execute("INSERT INTO papers VALUES ('upload-id','upload','data/uploads/upload-id.pdf',NULL)")
        conn.commit()

    result = retire_remote_fulltext(root, dry_run=False)

    assert result["db_paper_paths_cleared"] == 1
    with sqlite3.connect(db_path) as conn:
        network = conn.execute(
            "SELECT pdf_path, pdf_url FROM papers WHERE id='network'"
        ).fetchone()
        upload = conn.execute(
            "SELECT pdf_path, pdf_url FROM papers WHERE id='upload-id'"
        ).fetchone()
    assert network == (None, None)
    assert upload == ("data/uploads/upload-id.pdf", None)
