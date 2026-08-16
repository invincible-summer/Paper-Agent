"""Tests for the unified history store (DESIGN D-071)."""

import core.history_store as hs
from core.history_store import _resolve, HISTORY_DIR


def _isolated_store(monkeypatch, tmp_path):
    d = tmp_path / "history_record"
    d.mkdir()
    monkeypatch.setattr(hs, "HISTORY_DIR", d)
    return d


def test_save_and_load_round_trip(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fname = hs.save_session({"topic": "GNN", "messages": [{"role": "user", "content": "hi"}]})
    assert fname.startswith("chat_") and fname.endswith(".json")
    data = hs.load_session(fname)
    assert data is not None
    assert data["topic"] == "GNN"
    assert data["title"] == "GNN"  # derived from topic


def test_title_falls_back_to_first_user_message(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fname = hs.save_session({"messages": [{"role": "user", "content": "搜索RAG的论文\n详细点"}]})
    data = hs.load_session(fname)
    assert data["title"] == "搜索RAG的论文"


def test_title_defaults_to_new_chat(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fname = hs.save_session({})
    data = hs.load_session(fname)
    assert data["title"] == "新对话"


def test_rename_changes_title_not_filename(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fname = hs.save_session({"topic": "old"})
    renamed = hs.rename_session(fname, "我的综述草稿")
    assert renamed is not None
    assert renamed["title"] == "我的综述草稿"
    # filename unchanged
    data = hs.load_session(fname)
    assert data["title"] == "我的综述草稿"


def test_rename_empty_title_rejected(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fname = hs.save_session({"topic": "x"})
    assert hs.rename_session(fname, "   ") is None


def test_delete(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fname = hs.save_session({"topic": "x"})
    assert hs.delete_session(fname) is True
    assert hs.load_session(fname) is None
    assert hs.delete_session(fname) is False  # already gone


def test_list_sessions_returns_metadata(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    hs.save_session({"topic": "A", "papers": [{}, {}], "messages": [{"role": "user", "content": "m"}]})
    hs.save_session({"topic": "B", "literature_review": "x"})
    items = hs.list_sessions()
    assert len(items) == 2
    titles = {i["title"] for i in items}
    assert titles == {"A", "B"}
    a = next(i for i in items if i["title"] == "A")
    assert a["paper_count"] == 2
    assert a["message_count"] == 1


def test_trace_id_accumulation(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fname = hs.save_session({"topic": "x", "trace_ids": ["t1"]})
    hs.add_trace_id(fname, "t2")
    hs.add_trace_id(fname, "t1")  # dup ignored
    data = hs.load_session(fname)
    assert data["trace_ids"] == ["t1", "t2"]


def test_update_in_place_keeps_title(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fname = hs.save_session({"topic": "orig"})
    hs.rename_session(fname, "renamed")
    # later update without title field should keep the renamed title
    hs.save_session({"topic": "orig", "papers": [{}]}, filename=fname)
    data = hs.load_session(fname)
    assert data["title"] == "renamed"


def test_path_traversal_sanitized(monkeypatch, tmp_path):
    d = _isolated_store(monkeypatch, tmp_path)
    # a bare filename with path components is reduced to its name
    fname = hs.save_session({"topic": "x"})
    evil = "../../etc/passwd"
    assert hs.load_session(evil) is None  # not found, not traversed
    assert hs.delete_session(evil) is False
    assert (d.parent.parent / "etc" / "passwd").exists() is False
    _ = fname


def test_load_session_no_arbitrary_file_read(monkeypatch, tmp_path):
    """D-089 regression: load_session must NOT read files outside HISTORY_DIR.

    The old fallback opened any existing Path(filename); an attacker could
    GET /history/{filename:path} with a real path and read arbitrary JSON
    (e.g. frontend/package.json). After the fix only the basename is used and
    only files inside HISTORY_DIR are readable.
    """
    d = _isolated_store(monkeypatch, tmp_path)
    # Drop a JSON file OUTSIDE the history dir, reachable by a relative path.
    secret = tmp_path / "secret.json"
    secret.write_text('{"topic": "LEAKED", "messages": []}', encoding="utf-8")

    # Relative path that used to resolve via the fallback.
    rel = "../secret.json"
    assert _resolve(rel) == d / "secret.json"  # basename only, stays inside dir
    assert hs.load_session(rel) is None  # outside HISTORY_DIR -> not found

    # Absolute path must also be confined to HISTORY_DIR (basename only).
    assert hs.load_session(str(secret)) is None

    # A real session still loads fine.
    fname = hs.save_session({"topic": "ok"})
    assert hs.load_session(fname)["topic"] == "ok"


def test_derive_source_chat_only(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fname = hs.save_session({"topic": "x", "messages": [{"role": "user", "content": "hi"}]})
    items = hs.list_sessions()
    assert items[0]["source"] == "chat"


def test_derive_source_structured_only(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    hs.save_session({"topic": "x", "papers": [{}]})
    items = hs.list_sessions()
    assert items[0]["source"] == "structured"


def test_derive_source_both(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    hs.save_session({"topic": "x", "messages": [{"role": "user", "content": "hi"}], "papers": [{}], "literature_review": "review text", "graph_data": {"nodes": []}})
    items = hs.list_sessions()
    assert items[0]["source"] == "both"


def test_derive_source_empty_defaults_to_chat(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    hs.save_session({"topic": "x"})
    items = hs.list_sessions()
    assert items[0]["source"] == "chat"


def test_list_sessions_includes_source_field(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    hs.save_session({"topic": "x"})
    items = hs.list_sessions()
    assert "source" in items[0]
