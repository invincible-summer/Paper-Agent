from __future__ import annotations

from core.models import Paper, PaperSummary
from agents.session import ChatSession
import core.paper_search_settings_store as store
from agents import tools_impl
from agents.review_agent import _build_papers_list


def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(store, "_seed", lambda: store.PaperSearchPolicy(
        sources={name: True for name in store.SOURCE_IDS}))
    store.reset_cache()


def _close(source: str, capability: str):
    policy = store.get_paper_search_policy()
    return store.set_source_capability(source, capability, False,
                                       expected_version=policy.version, updated_by="test")


def test_abstract_gate_strips_raw_and_cached_abstract_summary(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    paper = Paper(id="P", title="Paper", source="arxiv", abstract="secret abstract")
    session = ChatSession(session_id="s", papers=[paper])
    session.paper_summaries[paper.id] = PaperSummary(
        paper_id=paper.id, research_problem="derived from abstract")
    _close("arxiv", "abstract")

    corpus = tools_impl._session_corpus(session)
    assert all("secret abstract" not in row["text"] for row in corpus)
    assert all("derived from abstract" not in row["text"] for row in corpus)
    prompt_text = _build_papers_list([paper], session.paper_summaries)
    assert "secret abstract" not in prompt_text
    assert "derived from abstract" not in prompt_text


def test_fulltext_summary_survives_later_abstract_closure(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    paper = Paper(id="P", title="Paper", source="arxiv", abstract="secret abstract")
    session = ChatSession(session_id="s", papers=[paper])
    session.paper_summaries[paper.id] = PaperSummary(
        paper_id=paper.id, research_problem="verified full evidence",
        full_text="x" * 1000,
        document_info={"read_level": "full", "text_chars": 1000},
    )
    _close("arxiv", "abstract")
    corpus = tools_impl._session_corpus(session)
    assert any("verified full evidence" in row["text"] for row in corpus)


def test_system_prompt_exposes_runtime_disabled_capability(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    _close("arxiv", "abstract")
    from core.prompts.system import get_system_prompt
    prompt = get_system_prompt()
    assert "arXiv.abstract" in prompt
    assert "不要尝试绕过或重复联网" in prompt


def test_deep_read_reports_structured_fulltext_skip_without_remote_access(tmp_path, monkeypatch):
    import asyncio
    from agents import reader_agent
    from core.models import PaperSummary
    from tools.pdf.fetcher import PDFFetcher

    _isolated(tmp_path, monkeypatch)
    paper = Paper(
        id="hash:capability-deep-read",
        title="Policy bounded paper",
        source="arxiv",
        abstract="allowed abstract",
        doi="10.1000/example",
        pdf_url="https://arxiv.org/pdf/1706.03762.pdf",
    )
    session = ChatSession(session_id="s", papers=[paper])
    _close("arxiv", "fulltext")

    async def forbidden_unpaywall(*_args, **_kwargs):
        raise AssertionError("Unpaywall must not run when fulltext is disabled")

    async def forbidden_download(*_args, **_kwargs):
        raise AssertionError("download must not run when fulltext is disabled")

    async def fake_process(paper, *_args, **_kwargs):
        return PaperSummary(
            paper_id=paper.id,
            research_problem="abstract-only evidence",
            document_info={"read_level": "abstract", "text_chars": 16},
        ), None

    monkeypatch.setattr(PDFFetcher, "_unpaywall_pdf_url", forbidden_unpaywall)
    monkeypatch.setattr(PDFFetcher, "_download", forbidden_download)
    monkeypatch.setattr(reader_agent, "_process_single_paper", fake_process)
    result = asyncio.run(tools_impl._tool_deep_read({"paper_ids": [paper.id]}, session, None))
    assert result.status == "success"
    skips = result.data["capability_skips"]
    assert len(skips) == 1
    assert skips[0]["source"] == "arxiv"
    assert skips[0]["capability"] == "fulltext"
    assert skips[0]["status"] == "skipped"
    assert skips[0]["reason_code"] == "source_capability_disabled"
    assert skips[0]["paper_id"] == paper.id
    assert skips[0]["title"] == paper.title
    assert result.data["abstract_fallback_papers"][0]["reason"] == "source_capability_disabled"


def test_ask_papers_reports_fulltext_skip_and_uses_only_allowed_abstract(tmp_path, monkeypatch):
    import asyncio

    _isolated(tmp_path, monkeypatch)
    paper = Paper(
        id="hash:capability-ask",
        title="Policy bounded paper",
        source="arxiv",
        abstract="allowed abstract evidence",
    )
    session = ChatSession(session_id="s", papers=[paper])
    session.paper_summaries[paper.id] = PaperSummary(
        paper_id=paper.id,
        research_problem="allowed abstract evidence",
    )
    _close("arxiv", "fulltext")

    def no_index(_session):
        return None

    async def same_query(query):
        return query

    async def answer(_query, _passages):
        return "Answer [P1]"

    monkeypatch.setattr(tools_impl, "_ensure_session_index", no_index)
    monkeypatch.setattr(tools_impl, "_rewrite_query", same_query)
    monkeypatch.setattr(tools_impl, "_retrieve_passages", lambda *_args, **_kwargs: [{
        "paper_id": paper.id,
        "title": paper.title,
        "text": "allowed abstract evidence",
        "score": 1.0,
    }])
    monkeypatch.setattr(tools_impl, "_generate_grounded_answer", answer)
    monkeypatch.setattr(tools_impl, "evidence_gap", lambda *_args, **_kwargs: False, raising=False)
    result = asyncio.run(tools_impl._tool_ask_papers({
        "query": "What is the evidence?", "paper_id": paper.id,
    }, session, None))
    assert result.status == "success"
    assert result.data["capability_skips"][0]["reason_code"] == "source_capability_disabled"
    assert "已跳过 OA 探测与下载" in result.text


def test_dedup_field_provenance_uses_evidence_source_not_every_merged_source(tmp_path, monkeypatch):
    import asyncio
    from tools.pdf.fetcher import PDFFetcher

    _isolated(tmp_path, monkeypatch)
    _close("arxiv", "abstract")
    current = store.get_paper_search_policy()
    store.set_source_capability(
        "arxiv", "fulltext", False,
        expected_version=current.version, updated_by="test",
    )
    paper = Paper(
        id="P", title="Merged", source="arxiv,openalex",
        abstract="OpenAlex abstract", abstract_source="openalex",
        pdf_url="https://example.org/openalex.pdf", pdf_source="openalex",
    )
    assert store.paper_abstract_text(paper) == "OpenAlex abstract"

    fetcher = PDFFetcher.__new__(PDFFetcher)
    fetcher.admin_override = False
    urls = asyncio.run(fetcher.candidate_urls(paper))
    assert urls == ["https://example.org/openalex.pdf"]


def test_paper_capability_provenance_round_trips_through_dict():
    paper = Paper(
        id="P", title="Provenance", source="arxiv,openalex",
        abstract="abstract", abstract_source="openalex",
        abstract_policy_status="disabled", abstract_policy_reason="policy",
        pdf_url="https://arxiv.org/pdf/1706.03762.pdf", pdf_source="arxiv",
    )
    restored = Paper.from_dict(paper.to_dict())
    assert restored.abstract_source == "openalex"
    assert restored.abstract_policy_status == "disabled"
    assert restored.abstract_policy_reason == "policy"
    assert restored.pdf_source == "arxiv"


def test_search_tool_returns_structured_error_when_every_platform_is_skipped(monkeypatch):
    import asyncio

    async def fake_search_agent(state, **_kwargs):
        return {
            **state,
            "papers": [], "candidates": [], "sub_directions": [],
            "search_queries": [state["topic"]],
            "search_route": {
                "primary": [], "fallback": [],
                "skipped": [{
                    "source": "arxiv", "capability": "search",
                    "status": "skipped",
                    "reason_code": "source_capability_disabled",
                    "reason": "arXiv 搜索能力已关闭",
                }],
            },
        }

    monkeypatch.setattr("agents.search_agent.search_agent", fake_search_agent)
    session = ChatSession(session_id="s")
    result = asyncio.run(tools_impl._tool_search_papers(
        {"topic": "test topic"}, session, None,
    ))
    assert result.error_code == "SOURCE_CAPABILITY_DISABLED"
    assert result.data["skipped_capabilities"][0]["status"] == "skipped"


def test_verify_fulltext_exposes_unpaywall_structured_skip(tmp_path, monkeypatch):
    import asyncio
    from tools.pdf.availability import verify_papers_fulltext
    import core.paper_search_settings_store as policy_store

    _isolated(tmp_path, monkeypatch)
    policy = policy_store.get_paper_search_policy()
    policy_store.set_source_capability(
        "unpaywall", "fulltext", False,
        expected_version=policy.version, updated_by="test",
    )
    paper = Paper(
        id="P", title="No direct PDF", source="openalex",
        doi="10.1/example", pdf_url=None,
    )
    fake_settings = type("Settings", (), {
        "search": type("Search", (), {
            "fulltext_verify_timeout_seconds": 5,
            "fulltext_status_ttl_days": 30,
        })(),
        "reader": type("Reader", (), {"pdf_dir": str(tmp_path / "pdfs")})(),
        "storage": type("Storage", (), {"sqlite_path": str(tmp_path / "metadata.db")})(),
    })()
    monkeypatch.setattr("tools.pdf.availability.get_settings", lambda: fake_settings)
    skips = []
    statuses = asyncio.run(verify_papers_fulltext(
        [paper], timeout_seconds=2, capability_skips=skips,
    ))
    assert statuses[paper.id] == "unknown"
    assert skips[0]["source"] == "unpaywall"
    assert skips[0]["reason_code"] == "source_capability_disabled"


def test_metadata_database_migrates_and_preserves_evidence_provenance(tmp_path):
    import sqlite3
    from tools.storage.database import Database

    db_path = tmp_path / "legacy-metadata.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE papers (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT,
                year INTEGER, venue TEXT, doi TEXT, source TEXT, language TEXT,
                citation_count INTEGER, abstract TEXT, pdf_url TEXT,
                pdf_path TEXT, keywords TEXT, urls TEXT
            )"""
        )

    db = Database(str(db_path))
    paper = Paper(
        id="P", title="Provenance", source="openalex", abstract="abstract",
        abstract_source="openaire", abstract_policy_status="disabled",
        abstract_policy_reason="diagnostic closure",
        pdf_url="https://arxiv.org/pdf/1706.03762.pdf", pdf_source="arxiv",
    )
    db.save_paper(paper)
    restored = db.get_paper(paper.id)
    db.close()

    assert restored is not None
    assert restored.abstract_source == "openaire"
    assert restored.abstract_policy_status == "disabled"
    assert restored.abstract_policy_reason == "diagnostic closure"
    assert restored.pdf_source == "arxiv"

    # Opening the already-migrated database again must remain idempotent.
    db = Database(str(db_path))
    assert db.get_paper(paper.id).pdf_source == "arxiv"
    db.close()


def test_successful_abstract_gate_recheck_clears_stale_disabled_marker(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    paper = Paper(
        id="P", title="Paper", source="arxiv", abstract="usable",
        abstract_policy_status="disabled", abstract_policy_reason="old failure",
    )
    assert store.paper_abstract_text(paper) == "usable"
    assert paper.abstract_policy_status == "available"
    assert paper.abstract_policy_reason == ""


def test_structured_fulltext_skip_uses_pdf_evidence_source(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    paper = Paper(
        id="P", title="Merged paper", source="openalex",
        pdf_url="https://arxiv.org/pdf/1706.03762.pdf", pdf_source="arxiv",
    )
    _close("arxiv", "fulltext")
    skip = tools_impl._fulltext_capability_skip(
        ChatSession(session_id="s", papers=[paper]), paper,
    )
    assert skip is not None
    assert skip["source"] == "arxiv"
    assert skip["paper_id"] == "P"


def test_research_map_returns_structured_network_capability_skips(tmp_path, monkeypatch):
    import asyncio

    _isolated(tmp_path, monkeypatch)
    _close("openalex", "search")
    _close("arxiv", "abstract")
    session = ChatSession(
        session_id="s",
        papers=[Paper(
            id="P", title="Policy map", source="arxiv",
            abstract="must not enter map evidence",
        )],
    )

    async def fake_map(*_args, **_kwargs):
        return {
            "clusters": [{"id": 0, "paper_ids": ["P"]}],
            "timeline": [], "landscape": "map", "graph": {"nodes": [], "edges": []},
            "degraded": False, "embedding_status": "available",
            "summary_status": "available", "citation_mode": "fast",
            "citation_enrichment_status": "source_capability_disabled",
            "stage_ms": {}, "degraded_reasons": [],
        }

    monkeypatch.setattr("agents.map_agent.build_research_map", fake_map)
    monkeypatch.setattr(
        "core.runtime_performance_policy.get_performance_policy",
        lambda: type("Policy", (), {"map_citation_mode": "fast"})(),
    )
    result = asyncio.run(tools_impl._tool_research_map({}, session, None))
    assert result.status == "success"
    skips = result.data["capability_skips"]
    assert {(row["source"], row["capability"]) for row in skips} == {
        ("openalex", "search"), ("arxiv", "abstract"),
    }
    assert all(row["status"] == "skipped" for row in skips)


def test_pubmed_abstract_closure_uses_esummary_not_efetch(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from tools.search import pubmed

    _isolated(tmp_path, monkeypatch)
    _close("pubmed", "abstract")

    class Response:
        def __init__(self, payload, host):
            self._payload = payload
            self.text = ""
            self.status_code = 200
            self.headers = {"content-type": "application/json"}
            self.history = []
            self.extensions = {}
            self.url = SimpleNamespace(host=host)

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.calls = []
            self.responses = [
                Response({"esearchresult": {"idlist": ["123"]}}, "eutils.ncbi.nlm.nih.gov"),
                Response({"result": {
                    "uids": ["123"],
                    "123": {
                        "uid": "123", "title": "Metadata only PubMed result",
                        "authors": [{"name": "A Author"}],
                        "pubdate": "2024", "fulljournalname": "Journal",
                        "articleids": [{"idtype": "doi", "value": "10.1/pubmed"}],
                    },
                }}, "eutils.ncbi.nlm.nih.gov"),
            ]

        async def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return self.responses.pop(0)

    class Limiter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetch(self, client, method, url, **kwargs):
            return await client.request(method, url, **kwargs)

    client = Client()
    settings = SimpleNamespace(search=SimpleNamespace(
        paper_platform_contact_email="ops@example.org",
        crossref_email="", openalex_email="", ncbi_api_key="",
    ))
    monkeypatch.setattr(pubmed, "get_search_http_client", lambda: client)
    monkeypatch.setattr(pubmed, "get_settings", lambda: settings)
    monkeypatch.setattr(pubmed, "_LIMITER_NO_KEY", Limiter())

    outcome = asyncio.run(pubmed.PubMedBackend().search_many(["cancer"], 5))
    assert outcome.status == "ok"
    assert [call[1] for call in client.calls] == [
        pubmed.ESEARCH_URL, pubmed.ESUMMARY_URL,
    ]
    assert all(call[1] != pubmed.EFETCH_URL for call in client.calls)
    assert outcome.papers[0].abstract == ""
    assert outcome.papers[0].title == "Metadata only PubMed result"
