"""Evidence and capability boundaries after remote full-text retirement."""

from __future__ import annotations

import asyncio
import sqlite3

from agents.session import ChatSession
from agents import tools_impl
from agents.review_agent import _build_papers_list
from core.models import Paper, PaperSummary
import core.paper_search_settings_store as store


def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(store, "_seed", lambda: store.PaperSearchPolicy(
        sources={name: True for name in store.SOURCE_IDS}))
    store.reset_cache()


def test_disabled_abstract_removes_network_paper_from_retrieval_and_prompt(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    paper = Paper(id="P", title="Paper", source="arxiv", abstract="secret abstract")
    session = ChatSession(session_id="s", papers=[paper])
    session.paper_summaries[paper.id] = PaperSummary(
        paper_id=paper.id, research_problem="derived from abstract")
    policy = store.get_paper_search_policy()
    store.set_source_capability(
        "arxiv", "abstract", False,
        expected_version=policy.version, updated_by="test",
    )
    corpus = tools_impl._session_corpus(session)
    assert all("secret abstract" not in row["text"] for row in corpus)
    assert all("derived from abstract" not in row["text"] for row in corpus)
    assert "secret abstract" not in _build_papers_list([paper], session.paper_summaries)


def test_network_fulltext_summary_never_bypasses_abstract_gate(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    paper = Paper(id="P", title="Paper", source="arxiv", abstract="abstract")
    session = ChatSession(session_id="s", papers=[paper])
    session.paper_summaries[paper.id] = PaperSummary(
        paper_id=paper.id, research_problem="legacy remote evidence",
        full_text="legacy body", document_info={"read_level": "full"},
    )
    policy = store.get_paper_search_policy()
    store.set_source_capability(
        "arxiv", "abstract", False,
        expected_version=policy.version, updated_by="test",
    )
    assert all("legacy remote evidence" not in row["text"] for row in tools_impl._session_corpus(session))


def test_network_deep_read_is_rejected_without_calling_reader(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("network reader must never be called")

    monkeypatch.setattr("agents.reader_agent.reader_agent", forbidden)
    session = ChatSession(papers=[Paper(id="P", title="Network")])
    result = asyncio.run(tools_impl._tool_deep_read({"paper_ids": ["P"]}, session, None))
    assert result.is_error and result.error_code == "VALIDATION_ERROR"
    assert "上传论文文件" in result.text


def test_ask_papers_uses_only_live_abstract(monkeypatch):
    paper = Paper(id="P", title="Paper", source="arxiv", abstract="live abstract")
    session = ChatSession(session_id="s", papers=[paper])
    session.paper_summaries[paper.id] = PaperSummary(
        paper_id=paper.id, research_problem="stale remote full text",
        full_text="stale body", document_info={"read_level": "full"},
    )
    monkeypatch.setattr(tools_impl, "_ensure_session_index", lambda _session: None)
    monkeypatch.setattr(tools_impl, "_rewrite_query", lambda query: asyncio.sleep(0, result=query))
    monkeypatch.setattr(tools_impl, "_retrieve_passages", lambda *_a, **_k: [{
        "paper_id": "P", "title": "Paper", "text": "live abstract", "score": 1.0,
    }])
    monkeypatch.setattr(tools_impl, "_generate_grounded_answer", lambda *_a, **_k: asyncio.sleep(0, result="answer [P]"))
    result = asyncio.run(tools_impl._tool_ask_papers({"query": "evidence", "paper_id": "P"}, session, None))
    assert result.status == "success"
    assert "stale remote" not in result.text


def test_paper_legacy_pdf_fields_are_cleared_on_restore():
    paper = Paper.from_dict({
        "id": "P", "title": "Legacy", "source": "arxiv",
        "pdf_url": "https://example.org/p.pdf", "pdf_source": "arxiv",
        "pdf_path": "/tmp/legacy.pdf", "fulltext_status": "available",
    })
    assert paper.pdf_url is None
    assert paper.pdf_source == ""
    assert paper.pdf_path is None
    assert paper.fulltext_status == "unknown"


def test_metadata_database_preserves_abstract_provenance_but_not_network_pdf(tmp_path):
    from tools.storage.database import Database

    db_path = tmp_path / "legacy-metadata.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE papers (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT,
            year INTEGER, venue TEXT, doi TEXT, source TEXT, language TEXT,
            citation_count INTEGER, abstract TEXT, pdf_url TEXT,
            pdf_path TEXT, keywords TEXT, urls TEXT)""")
    db = Database(str(db_path))
    db.save_paper(Paper(
        id="P", title="Provenance", source="openalex", abstract="abstract",
        abstract_source="openaire", abstract_policy_status="disabled",
        abstract_policy_reason="diagnostic closure", pdf_source="arxiv",
    ))
    restored = db.get_paper("P")
    db.close()
    assert restored is not None
    assert restored.abstract_source == "openaire"
    assert restored.abstract_policy_status == "disabled"
    assert restored.pdf_source == ""


def test_search_with_no_sources_returns_structured_error(monkeypatch):
    async def fake_search_agent(state, **_kwargs):
        return {
            **state, "papers": [], "candidates": [], "sub_directions": [],
            "search_queries": [state["topic"]],
            "search_route": {"primary": [], "fallback": [], "skipped": [{
                "source": "arxiv", "capability": "search", "status": "skipped",
                "reason_code": "source_capability_disabled", "reason": "关闭",
            }]},
        }

    monkeypatch.setattr("agents.search_agent.search_agent", fake_search_agent)
    result = asyncio.run(tools_impl._tool_search_papers({"topic": "test"}, ChatSession(), None))
    assert result.error_code == "SOURCE_CAPABILITY_DISABLED"


def test_abstract_gate_recheck_clears_stale_disabled_marker(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    paper = Paper(
        id="P", title="Paper", source="arxiv", abstract="usable",
        abstract_policy_status="disabled", abstract_policy_reason="old failure",
    )
    assert store.paper_abstract_text(paper) == "usable"
    assert paper.abstract_policy_status == "available"
    assert paper.abstract_policy_reason == ""
