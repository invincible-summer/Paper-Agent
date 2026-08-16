"""1h cross-modal retrieval tests (offline; vector store + LLM stubbed).

Covers the three pieces 1h added to ask_papers:
- _detect_modality: rule-based figure/table/formula cue detection (incl. the
  "表示" false-positive guard),
- element caption passages entering the BM25 corpus,
- the global elements vector track (kind-filtered by modality) fusing into RRF
  alongside text chunks, with session paper-set isolation preserved.
"""

from __future__ import annotations

import asyncio

import agents.tools_impl as ti
from core.models import Paper, PaperSummary
import tools.storage.vectorstore as vsm
from tools.storage.vectorstore import SearchHit


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeStore:
    """VectorStore stub that also serves the elements collection."""

    def __init__(self, chunks=None, elements=None, available=True):
        self._chunks = chunks or []
        self._elements = elements or []
        self._available = available
        self.upserted = []
        self.element_queries: list[tuple] = []  # (query, paper_ids, kind)

    def available(self):
        return self._available

    def search_chunks(self, query, session_id=None, paper_id=None, top_k=5):
        hits = self._chunks
        if paper_id is not None:
            hits = [h for h in hits if h.metadata.get("paper_id") == paper_id]
        return hits[:top_k]

    def search_summaries(self, query, session_id=None, top_k=20):
        return []

    def search_elements(self, query, paper_ids, kind=None, top_k=5):
        self.element_queries.append((query, list(paper_ids or []), kind))
        hits = self._elements
        if kind:
            hits = [h for h in hits if h.metadata.get("kind") == kind]
        if paper_ids:
            hits = [h for h in hits if h.metadata.get("paper_id") in set(paper_ids)]
        return hits[:top_k]

    def count_papers(self, session_id=None):
        return 1

    def upsert_paper_summary(self, paper, summary=None, session_id=""):
        self.upserted.append((paper.id, session_id))
        return True


def _ehit(eid, pid, kind, doc, page=1, title="T"):
    return SearchHit(
        id=eid, document=doc,
        metadata={"paper_id": pid, "kind": kind, "element_id": eid,
                  "page": page, "title": title},
    )


def _session(papers=None, summaries=None):
    s = ti.ChatSession()
    s.papers = papers or []
    if summaries:
        s.paper_summaries = summaries
    return s


def _patch(monkeypatch, store, llm_answer="A [P1]."):
    class _FakeLLM:
        async def ainvoke(self, messages):
            return _Resp(llm_answer)

    monkeypatch.setattr(vsm, "VectorStore", lambda *a, **k: store)
    monkeypatch.setattr(ti, "get_llm", lambda tier="light": _FakeLLM())

    async def _noop_fulltext(session, paper_id, progress_cb=None):
        return None

    monkeypatch.setattr(ti, "_ensure_fulltext", _noop_fulltext)
    monkeypatch.setattr(ti, "_rewrite_query", _identity_rewrite)
    import tools.retrieval.rerank as rr
    monkeypatch.setattr(rr, "get_reranker", lambda: None)


async def _identity_rewrite(query):
    return query


# --- _detect_modality (pure) --------------------------------------------------

def test_modality_detects_figure():
    assert ti._detect_modality("请解释 Figure 3 的结构") == "figure"
    assert ti._detect_modality("如图所示的趋势") == "figure"
    assert ti._detect_modality("这个 architecture diagram 怎么理解") == "figure"


def test_modality_detects_table():
    assert ti._detect_modality("对比 Table 2 的结果") == "table"
    assert ti._detect_modality("实验结果表格里的数据") == "table"


def test_modality_detects_formula():
    assert ti._detect_modality("损失函数的公式含义") == "formula"
    assert ti._detect_modality("explain equation 5") == "formula"


def test_modality_figure_wins_over_formula_for_container():
    """'图3 里的公式' addresses the figure (container), not the formula inside."""
    assert ti._detect_modality("图3 里的公式") == "figure"


def test_modality_none_for_plain_questions():
    assert ti._detect_modality("这个方法的效果怎么样") is None
    assert ti._detect_modality("") is None
    assert ti._detect_modality(None) is None


def test_modality_bare_biao_false_positive_guard():
    """'表示' (express) contains '表' but must NOT trigger table modality."""
    assert ti._detect_modality("这张图表示了注意力的计算流程") == "figure"
    # '表示' alone, no real table cue -> None (figure matched via '图')
    # A question with neither figure nor table cue, only '表示':
    assert ti._detect_modality("数据表示了什么含义") is None


# --- element caption passages in BM25 corpus ----------------------------------

def test_session_corpus_includes_element_captions():
    summary = PaperSummary(paper_id="P1")
    summary.elements = [
        {"element_id": "P1::figure::1", "kind": "figure", "caption": "Model architecture"},
        {"element_id": "P1::table::1", "kind": "table", "caption": "Ablation results"},
        {"element_id": "P1::figure::2", "kind": "figure", "caption": ""},  # no caption -> skip
    ]
    session = _session(papers=[Paper(id="P1", title="T")], summaries={"P1": summary})
    corpus = ti._session_corpus(session)
    by_id = {c["id"]: c for c in corpus}
    fig = by_id.get("P1::figure::1::element")
    assert fig is not None
    assert fig["section"] == "figure"
    assert "Model architecture" in fig["text"]
    assert by_id.get("P1::table::1::element") is not None
    # Element without a caption carries no lexical signal -> not in BM25 corpus.
    assert "P1::figure::2::element" not in by_id


# --- elements vector track + modality filter + RRF fusion ---------------------

def test_element_vector_track_surfaces_element_passage(monkeypatch):
    elements = [_ehit("P1::figure::1", "P1", "figure",
                      "[Caption] Arch\n[description] encoder-decoder diagram")]
    store = _FakeStore(elements=elements)
    _patch(monkeypatch, store, llm_answer="Figure [P1] shows the arch.")
    session = _session(papers=[Paper(id="P1", title="T")])
    result = asyncio.run(ti._tool_ask_papers({"query": "explain Figure 1"}, session, None))
    assert not result.is_error
    # search_elements was called with the figure kind filter.
    assert store.element_queries
    assert store.element_queries[0][2] == "figure"
    # The element passage reached the grounded context (answer succeeded).
    assert result.data["answer"]


def test_modality_none_searches_all_element_kinds(monkeypatch):
    elements = [
        _ehit("P1::formula::1", "P1", "formula", "[Formula] L = -sum(y log p)"),
    ]
    store = _FakeStore(elements=elements)
    _patch(monkeypatch, store)
    session = _session(papers=[Paper(id="P1", title="T")])
    asyncio.run(ti._tool_ask_papers({"query": "how is the loss computed"}, session, None))
    assert store.element_queries
    assert store.element_queries[0][2] is None  # no kind filter


def test_element_track_isolated_to_session_papers(monkeypatch):
    """The elements collection is global; only the session's paper set is queried."""
    elements = [
        _ehit("P1::figure::1", "P1", "figure", "P1 figure"),
        _ehit("P2::figure::1", "P2", "figure", "P2 figure"),  # not in session
    ]
    store = _FakeStore(elements=elements)
    _patch(monkeypatch, store)
    session = _session(papers=[Paper(id="P1", title="T")])  # only P1
    asyncio.run(ti._tool_ask_papers({"query": "Figure 1"}, session, None))
    assert store.element_queries
    queried = set(store.element_queries[0][1])
    assert queried == {"P1"}  # P2 never queried -> session isolation holds


def test_element_and_chunk_fuse_in_rrf(monkeypatch):
    """An element hit and a text-chunk hit both surface as distinct passages."""
    chunks = [
        SearchHit(id="P1::c0", document="method uses attention",
                  metadata={"paper_id": "P1", "title": "T", "section": "Method"})]
    elements = [_ehit("P1::figure::1", "P1", "figure",
                      "[Caption] Attention architecture\n[description] encoder")]
    store = _FakeStore(chunks=chunks, elements=elements)
    _patch(monkeypatch, store, llm_answer="A [P1].")
    session = _session(papers=[Paper(id="P1", title="T")])
    passages = ti._hybrid_retrieve(session, "attention architecture",
                                   paper_id=None, top_k=5)
    # Both a chunk passage and an element passage are present.
    element_passages = [p for p in passages if p.get("element_id")]
    assert any(p.get("element_id") == "P1::figure::1" for p in element_passages)
    assert any(not p.get("element_id") for p in passages)  # the text chunk


def test_upload_element_scope_is_session_isolated(monkeypatch):
    """Global upload elements are queried only for attachment ids in this session."""
    own = "a" * 32
    foreign = "b" * 32
    elements = [
        _ehit(f"upload:{own}::figure::1", f"upload:{own}", "figure", "own figure"),
        _ehit(f"upload:{foreign}::figure::1", f"upload:{foreign}", "figure", "foreign figure"),
    ]
    store = _FakeStore(elements=elements)
    _patch(monkeypatch, store)
    session = _session()
    session.attachments = [{
        "id": own, "filename": "own.png", "ext": "png", "multimodal_status": "ready"
    }]
    ti._hybrid_retrieve(session, "Figure 1", paper_id=None, top_k=5, modality="figure")
    assert store.element_queries
    assert store.element_queries[0][1] == [f"upload:{own}"]
