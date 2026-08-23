"""ask_papers tool tests (offline; vector store + LLM stubbed).

Covers the hybrid retrieval path, the no-data guard, the anti-hallucination
citation post-pass, single-paper scoping, and the BM25-only fallback when
the vector store is unavailable.
"""

from __future__ import annotations

import asyncio

import pytest

import agents.tools_impl as ti
from core.models import Paper, PaperSummary
import tools.storage.vectorstore as vsm
from tools.storage.vectorstore import SearchHit


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeStore:
    """Controllable VectorStore stub (session-scoped signatures)."""

    def __init__(self, summaries=None, chunks=None, available=True):
        self._summaries = summaries or []
        self._chunks = chunks or []
        self._available = available
        self.upserted = []

    def available(self):
        return self._available

    def search_summaries(self, query, session_id=None, top_k=20):
        return self._summaries[:top_k]

    def search_chunks(self, query, session_id=None, paper_id=None, top_k=5):
        hits = self._chunks
        if paper_id is not None:
            hits = [h for h in hits if h.metadata.get("paper_id") == paper_id]
        return hits[:top_k]

    def count_papers(self, session_id=None):
        return len(self._summaries)

    def upsert_paper_summary(self, paper, summary=None, session_id=""):
        self.upserted.append((paper.id, session_id))
        return True


def _hit(doc_id, pid, title, section="", doc=""):
    return SearchHit(id=doc_id, document=doc,
                     metadata={"paper_id": pid, "title": title, "section": section})


def _session(papers=None, summaries=None):
    s = ti.ChatSession()
    s.papers = papers or []
    if summaries:
        s.paper_summaries = summaries
    return s


def _patch(monkeypatch, store, llm_answer="ok"):
    class _FakeLLM:
        async def ainvoke(self, messages):
            return _Resp(llm_answer)

    monkeypatch.setattr(vsm, "VectorStore", lambda *a, **k: store)
    monkeypatch.setattr(ti, "get_llm", lambda tier="light": _FakeLLM())

    async def _identity_rewrite(query):
        return query
    monkeypatch.setattr(ti, "_rewrite_query", _identity_rewrite)

    # Reranker disabled: passages keep their RRF order.
    import tools.retrieval.rerank as rr
    monkeypatch.setattr(rr, "get_reranker", lambda: None)


_PAPERS = [Paper(id="P1", title="Attention Paper", abstract="attention mechanisms"),
           Paper(id="P2", title="RL Paper", abstract="policy gradient methods")]


def test_no_data_returns_no_papers(monkeypatch):
    store = _FakeStore(available=False)
    _patch(monkeypatch, store)
    session = _session()  # no papers, no summaries, no attachments
    result = asyncio.run(ti._tool_ask_papers({"query": "anything"}, session, None))
    assert result.is_error
    assert result.error_code == "NO_PAPERS"


def test_missing_query_validation_error(monkeypatch):
    _patch(monkeypatch, _FakeStore())
    session = _session(papers=_PAPERS)
    result = asyncio.run(ti._tool_ask_papers({}, session, None))
    assert result.is_error
    assert result.error_code == "VALIDATION_ERROR"
    assert "query" in result.error["message"]


def test_vector_track_retrieval_and_answer(monkeypatch):
    chunks = [
        _hit("P1::c0", "P1", "Attention Paper", "Method", "uses scaled dot-product attention"),
        _hit("P2::c0", "P2", "RL Paper", "Method", "policy gradient"),
    ]
    store = _FakeStore(summaries=chunks)
    _patch(monkeypatch, store, llm_answer="Answer cites [P1] and [P2].")
    session = _session(papers=_PAPERS)
    result = asyncio.run(ti._tool_ask_papers({"query": "what methods"}, session, None))
    assert not result.is_error
    assert result.data["answer"] == "Answer cites [P1] and [P2]."
    source_ids = {s["paper_id"] for s in result.data["sources"]}
    assert source_ids == {"P1", "P2"}


def test_citation_hallucination_stripped(monkeypatch):
    chunks = [_hit("P1::c0", "P1", "Paper One", "Method", "method A")]
    store = _FakeStore(summaries=chunks)
    _patch(monkeypatch, store,
           llm_answer="Based on [P1] and [doi:10.9999/fake] we conclude X.")
    session = _session(papers=[Paper(id="P1", title="Paper One", abstract="method A")])
    result = asyncio.run(ti._tool_ask_papers({"query": "q"}, session, None))
    assert "[P1]" in result.data["answer"]
    assert "[doi:10.9999/fake]" not in result.data["answer"]
    assert "证据不足" in result.data["answer"]


def test_paper_id_filter(monkeypatch):
    chunks = [
        _hit("P1::c0", "P1", "T1", "Method", "target method detail"),
        _hit("P2::c0", "P2", "T2", "Method", "other paper detail"),
    ]
    store = _FakeStore(summaries=chunks)
    _patch(monkeypatch, store, llm_answer="Only [P1].")
    session = _session(papers=_PAPERS)
    result = asyncio.run(ti._tool_ask_papers({"query": "q", "paper_id": "P1"}, session, None))
    assert not result.is_error
    assert result.data["answer"] == "Only [P1]."
    assert {s["paper_id"] for s in result.data["sources"]} == {"P1"}


def test_paper_id_abstract_detail_gap_is_stated_in_tool_text(monkeypatch):
    """Detailed questions stay bounded by the network paper's abstract."""
    store = _FakeStore(summaries=[_hit("P1", "P1", "Paywalled Paper",
                                       "abstract", "abstract only evidence")])
    _patch(monkeypatch, store, llm_answer="Abstract evidence [P1].")
    session = _session(papers=[Paper(id="P1", title="Paywalled Paper",
                                     abstract="abstract only evidence")])
    session.paper_summaries = {"P1": PaperSummary(paper_id="P1", methodology="m")}

    result = asyncio.run(ti._tool_ask_papers(
        {"query": "exact dataset size", "paper_id": "P1"}, session, None))

    assert not result.is_error
    assert "仅依据有效摘要" in result.text
    assert "上传论文原文" in result.text


def test_no_relevant_passages_partial(monkeypatch):
    """Store available but no hits AND empty corpus -> partial result."""
    _patch(monkeypatch, _FakeStore())
    session = _session(papers=[])  # guard needs something: give summaries instead
    session.paper_summaries = {"S1": PaperSummary(paper_id="S1")}
    # Make BM25 corpus empty by clearing summary fields (blank body) and no papers
    session.paper_summaries = {}
    session.attachments = []
    session.papers = [Paper(id="P9", title="", abstract="")]  # passes guard, empty corpus
    result = asyncio.run(ti._tool_ask_papers({"query": "q"}, session, None))
    assert result.status == "partial"
    assert result.data["answer"] == ""
    assert result.data["sources"] == []


def test_bm25_fallback_without_store(monkeypatch):
    """No vector store: the in-memory BM25 track still grounds an answer."""
    store = _FakeStore(available=False)
    _patch(monkeypatch, store, llm_answer="From [S1].")
    summary = PaperSummary(paper_id="S1", research_problem="the core problem",
                           methodology="m1")
    session = _session(papers=[Paper(id="S1", title="S1", abstract="the core problem m1")])
    result = asyncio.run(ti._tool_ask_papers({"query": "problem"}, session, None))
    assert not result.is_error
    assert result.data["answer"] == "From [S1]."
    assert {s["paper_id"] for s in result.data["sources"]} == {"S1"}


def test_tool_result_message_includes_answer(monkeypatch):
    from agents.orchestrator import _build_tool_result_message
    chunks = [_hit("P1::c0", "P1", "T", "Method", "m")]
    _patch(monkeypatch, _FakeStore(summaries=chunks),
           llm_answer="A long grounded answer body.")
    session = _session(papers=[Paper(id="P1", title="T", abstract="m")])
    result = asyncio.run(ti._tool_ask_papers({"query": "q"}, session, None))
    msg = _build_tool_result_message("ask_papers", result)
    assert "A long grounded answer body." in msg


def test_rewritten_query_feeds_retrieval_not_generation(monkeypatch):
    """The rewritten query drives retrieval; the original question drives QA."""
    seen = {"retrieval": [], "generation": []}

    class _RecordingStore(_FakeStore):
        def search_summaries(self, query, session_id=None, top_k=20):
            seen["retrieval"].append(query)
            return super().search_summaries(query, session_id=session_id, top_k=top_k)

    class _RecordingLLM:
        async def ainvoke(self, messages):
            seen["generation"].append(messages[0].content)
            return _Resp("Answer [P1].")

    store = _RecordingStore(summaries=[_hit("P1", "P1", "T", "abstract", "m")])
    monkeypatch.setattr(vsm, "VectorStore", lambda *a, **k: store)
    monkeypatch.setattr(ti, "get_llm", lambda tier="light": _RecordingLLM())

    async def _rewrite(query):
        return "rewritten academic keywords"
    monkeypatch.setattr(ti, "_rewrite_query", _rewrite)
    import tools.retrieval.rerank as rr
    monkeypatch.setattr(rr, "get_reranker", lambda: None)

    session = _session(papers=[Paper(id="P1", title="T", abstract="m")])
    result = asyncio.run(ti._tool_ask_papers({"query": "口语化的问题"}, session, None))
    assert not result.is_error
    assert seen["retrieval"] == ["rewritten academic keywords"]
    assert "口语化的问题" in seen["generation"][-1]


def test_rewrite_failure_falls_back_to_raw_query(monkeypatch):
    """Real _rewrite_query under a failing LLM: retrieval uses the raw query."""
    seen = {"retrieval": []}

    class _RecordingStore(_FakeStore):
        def search_chunks(self, query, session_id=None, paper_id=None, top_k=5):
            seen["retrieval"].append(query)
            return super().search_chunks(query, session_id=session_id,
                                         paper_id=paper_id, top_k=top_k)

    class _SelectiveLLM:
        async def ainvoke(self, messages):
            if "Rewrite the user's question" in messages[0].content:
                raise RuntimeError("llm down")
            return _Resp("A [P1].")

    store = _RecordingStore(chunks=[_hit("P1::c0", "P1", "T", "Method", "m")])
    monkeypatch.setattr(vsm, "VectorStore", lambda *a, **k: store)
    monkeypatch.setattr(ti, "get_llm", lambda tier="light": _SelectiveLLM())

    import tools.retrieval.rerank as rr
    monkeypatch.setattr(rr, "get_reranker", lambda: None)

    session = _session(papers=[Paper(id="P1", title="T", abstract="m")])
    result = asyncio.run(ti._tool_ask_papers({"query": "raw question"}, session, None))
    assert not result.is_error
    assert seen["retrieval"] == ["raw question"]


def test_parent_dedup_and_parent_text_used(monkeypatch):
    """Two child hits of one parent collapse to a single parent-text passage."""
    aid = "a" * 32
    parent = "Full parent passage with complete context about attention."
    hits = [
        SearchHit(id=f"sess::{aid}::c0", document="child fragment 1",
                  metadata={"paper_id": aid, "title": "T", "section": "Method",
                            "parent_id": "s0::p0", "parent_text": parent}),
        SearchHit(id=f"sess::{aid}::c1", document="child fragment 2",
                  metadata={"paper_id": aid, "title": "T", "section": "Method",
                            "parent_id": "s0::p0", "parent_text": parent}),
    ]
    seen = {"generation": []}

    class _RecordingLLM:
        async def ainvoke(self, messages):
            seen["generation"].append(messages[0].content)
            return _Resp(f"Answer [{aid}].")

    _patch(monkeypatch, _FakeStore(chunks=hits))
    monkeypatch.setattr(ti, "get_llm", lambda tier="light": _RecordingLLM())
    session = _session()
    session.attachments = [{"id": aid, "filename": "paper.txt", "ext": "txt"}]
    result = asyncio.run(ti._tool_ask_papers({"query": "q"}, session, None))
    assert not result.is_error
    ctx = seen["generation"][-1]
    assert parent in ctx
    assert "child fragment" not in ctx  # parent text replaces child fragments
    assert ctx.count(parent) == 1       # parent deduplicated


def test_rerank_order_applied(monkeypatch):
    """When the reranker is available its ordering wins over RRF order."""
    class _ReverseRanker:
        def predict(self, pairs):
            # reverse relevance: later passages score higher
            return [float(i) for i, _ in enumerate(pairs)]

    import tools.retrieval.rerank as rr
    monkeypatch.setattr(rr, "get_reranker", lambda: _ReverseRanker())
    passages = [
        {"paper_id": "P1", "title": "T1", "section": "abstract", "text": "first passage"},
        {"paper_id": "P2", "title": "T2", "section": "abstract", "text": "second passage"},
    ]
    ranked = ti._rerank_passages("q", passages, 2)
    assert [item["text"] for item in ranked] == ["second passage", "first passage"]


def test_index_self_heal_reindexes_empty_session(monkeypatch):
    """Chroma has nothing for this session -> summaries re-indexed on the fly."""
    store = _FakeStore(chunks=[_hit("P1::c0", "P1", "T", "Method", "m")])
    _patch(monkeypatch, store, llm_answer="A [P1].")
    session = _session(papers=_PAPERS)
    result = asyncio.run(ti._tool_ask_papers({"query": "q"}, session, None))
    assert not result.is_error
    assert {pid for pid, _ in store.upserted} == {"P1", "P2"}
    assert all(sid == session.session_id for _, sid in store.upserted)


def test_index_self_heal_skipped_when_index_populated(monkeypatch):
    store = _FakeStore(summaries=[_hit("P1", "P1", "T", "", "doc")],
                       chunks=[_hit("P1::c0", "P1", "T", "Method", "m")])
    _patch(monkeypatch, store, llm_answer="A [P1].")
    session = _session(papers=_PAPERS)
    result = asyncio.run(ti._tool_ask_papers({"query": "q"}, session, None))
    assert not result.is_error
    assert store.upserted == []  # already indexed — no redundant work


def test_network_structure_query_ignores_legacy_fulltext_outline(monkeypatch):
    """A restored network outline must never bypass the abstract boundary."""
    chunks = [_hit("P1::c0", "P1", "PinSage", "3.4 Node Embeddings via MapReduce",
                   "MapReduce inference details")]
    store = _FakeStore(summaries=chunks)
    captured = {}

    async def fake_generate(query, passages):
        captured["passages"] = passages
        titles = [line for line in passages[0]["text"].splitlines() if line.startswith("-")]
        return "；".join(titles) + " [P1]"

    _patch(monkeypatch, store)
    monkeypatch.setattr(ti, "_generate_grounded_answer", fake_generate)
    summary = PaperSummary(
        paper_id="P1", full_text="full body " * 80,
        section_outline=[
            {"title": "1 INTRODUCTION", "page_start": 1, "page_end": 2},
            {"title": "2 RELATED WORK", "page_start": 2, "page_end": 3},
            {"title": "3 METHOD", "page_start": 3, "page_end": 3},
            *[{"title": f"3.{i} Subsection", "page_start": 3+i//2, "page_end": 3+i//2}
              for i in range(1, 6)],
            {"title": "4 EXPERIMENTS", "page_start": 7, "page_end": 7},
            *[{"title": f"4.{i} Experiment", "page_start": 7+i//2, "page_end": 7+i//2}
              for i in range(1, 6)],
            {"title": "5 CONCLUSION", "page_start": 10, "page_end": 10},
        ],
        document_info={"read_level": "full", "section_count": 15},
    )
    session = _session(
        papers=[Paper(id="P1", title="PinSage", abstract="paper organization and method")],
        summaries={"P1": summary},
    )
    result = asyncio.run(ti._tool_ask_papers(
        {"query": "仔细说明这篇论文有哪些部分，每个章节讲什么", "paper_id": "P1", "top_k": 3},
        session, None,
    ))
    assert not result.is_error
    assert captured["passages"][0]["section"] == "abstract"
    assert "1 INTRODUCTION" not in captured["passages"][0]["text"]
    assert result.data["sources"][0]["sections"][0] == "abstract"


def test_uploaded_structure_outline_remains_available():
    aid = "b" * 32
    doc_id = f"upload:{aid}"
    session = _session(summaries={
        doc_id: PaperSummary(
            paper_id=doc_id,
            section_outline=[
                {"title": "1 INTRODUCTION", "page_start": 1, "page_end": 2},
                {"title": "2 METHOD", "page_start": 3, "page_end": 4},
            ],
        )
    })
    session.attachments = [{"id": aid, "filename": "上传论文.pdf", "ext": "pdf"}]
    passage = ti._section_outline_passage(session, doc_id)
    assert passage is not None
    assert passage["section"] == "section_outline"
    assert "1 INTRODUCTION" in passage["text"]
    assert "2 METHOD" in passage["text"]


def test_deferred_attachment_rag_returns_explicit_degradation(monkeypatch):
    import tools.ingest.attachments as attachment_ingest

    store = _FakeStore(chunks=[])
    _patch(monkeypatch, store)

    async def fake_understand(attachment, session, *, focus=""):
        return {
            "id": attachment["id"], "status": "deferred", "element_count": 0,
            "reason": "该格式已安全保存，但当前版本尚未提供解析能力。",
        }

    monkeypatch.setattr(attachment_ingest, "ensure_attachment_understood", fake_understand)
    session = _session()
    session.attachments = [{
        "id": "d" * 32, "filename": "sheet.xlsx", "ext": "xlsx",
        "multimodal_status": "deferred",
    }]
    result = asyncio.run(ti._tool_ask_papers({
        "query": "总结这个表格", "attachment_id": "d" * 32,
    }, session, None))
    assert not result.is_error
    assert result.status == "partial"
    assert "尚未提供 DOC/XLS/XLSX 解析能力" in result.text
    assert result.data["attachments"][0]["status"] == "deferred"
