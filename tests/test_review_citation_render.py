"""D-091: literature review citation rendering.

The review LLM emits [paper_id] tokens (anti-hallucination keys on stable
ids). review_agent post-processes the assembled review to render each valid
[paper_id] as `Title ([Link](url))`, leaving unknown ids and
[CITATION NEEDED] untouched. Covers the pure helper + the full pipeline with
a stubbed LLM so no network/tokens are spent.
"""
import asyncio

import pytest

from agents.review_agent import _paper_link, _render_citations_as_titles, review_agent
from core.models import Paper


def _mk(id_, title, **kw):
    return Paper(id=id_, title=title, **kw)


def test_render_replaces_known_ids_with_title_and_link():
    p1 = _mk("aaa", "Enlightenment Reason", doi="10.1/x", urls={"openalex": "https://openalex.org/W1"})
    p2 = _mk("bbb", "Feminist Theory", urls={"arxiv": "https://arxiv.org/abs/1"})
    p3 = _mk("ccc", "No Link Paper")
    m = {p.id: p for p in (p1, p2, p3)}
    text = "Smith [aaa] and [bbb]; unknown [zzz] stays; [CITATION NEEDED] stays; [ccc] last."
    out = _render_citations_as_titles(text, m)
    assert "Enlightenment Reason ([Link](https://openalex.org/W1))" in out
    assert "Feminist Theory ([Link](https://arxiv.org/abs/1))" in out
    assert "No Link Paper" in out
    # Unknown id token and the anti-hallucination marker must survive untouched.
    assert "[zzz]" in out
    assert "[CITATION NEEDED]" in out


def test_paper_link_prefers_landing_over_pdf():
    p = _mk("x", "T", doi="10.1/y", urls={"doi": "https://doi.org/10.1/y"},
            pdf_url="https://example.org/x.pdf")
    assert _paper_link(p) == "https://doi.org/10.1/y"


def test_paper_link_falls_back_to_doi_resolver():
    assert _paper_link(_mk("x", "T", doi="10.1/z")) == "https://doi.org/10.1/z"


def test_paper_link_none_when_nothing_available():
    assert _paper_link(_mk("x", "T")) is None


def _stub_llm(monkeypatch):
    """Make every _write_section / _review_and_revise return canned text that
    contains [paper_id] tokens, so the pipeline is deterministic and offline."""
    from langchain_core.messages import HumanMessage

    class _Resp:
        def __init__(self, content):
            self.content = content

    async def fake_ainvoke(self, messages):  # noqa: ANN001
        # Echo a section that cites both papers by id + one bogus id.
        return _Resp("A claim [aaa]. Another [bbb]. A bogus [zzz]. [CITATION NEEDED].")

    class _FakeLLM:
        async def ainvoke(self, messages):
            return await fake_ainvoke(self, messages)

    import agents.review_agent as ra
    monkeypatch.setattr(ra, "get_llm", lambda name="light": _FakeLLM())


def test_review_agent_renders_citations_as_titles(monkeypatch):
    _stub_llm(monkeypatch)
    p1 = _mk("aaa", "Enlightenment Reason", urls={"openalex": "https://openalex.org/W1"})
    p2 = _mk("bbb", "Feminist Theory", urls={"arxiv": "https://arxiv.org/abs/1"})
    state = {
        "topic": "test",
        "user_conception": "",
        "papers": [p1, p2],
        "paper_summaries": {},
        "citation_graph_data": {},
        "review_rounds": 0,
        "review_cluster_count": 3,
    }
    result = asyncio.run(review_agent(state))
    review = result["literature_review"]
    # Known ids are rendered as Title ([Link](url)).
    assert "Enlightenment Reason ([Link](https://openalex.org/W1))" in review
    assert "Feminist Theory ([Link](https://arxiv.org/abs/1))" in review
    # Unknown id and the marker are left intact (no title, no link).
    assert "[zzz]" in review
    assert "[CITATION NEEDED]" in review
    # No raw known-id token should remain.
    assert "[aaa]" not in review
    assert "[bbb]" not in review
