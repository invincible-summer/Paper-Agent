"""Session-scoped paper identifier resolution."""

from agents.session import ChatSession
from core.models import Paper


def test_paper_by_id_prefers_exact_stable_id():
    exact = Paper(id="P1", title="Exact")
    session = ChatSession(papers=[exact])

    assert session.paper_by_id("P1") is exact
    assert session.paper_by_id("p1") is None


def test_paper_by_id_accepts_unique_normalized_doi_alias():
    paper = Paper(
        id="doi:10.48550/arXiv.1706.03762",
        doi="10.48550/arXiv.1706.03762",
        title="Attention Is All You Need",
    )
    session = ChatSession(papers=[paper])

    assert session.paper_by_id("10.48550/arxiv.1706.03762") is paper
    assert session.paper_by_id("https://doi.org/10.48550/ARXIV.1706.03762/") is paper


def test_paper_by_id_rejects_unknown_or_ambiguous_doi_alias():
    first = Paper(id="first", doi="10.1000/DUP", title="First")
    second = Paper(id="second", doi="10.1000/dup", title="Second")
    session = ChatSession(papers=[first, second])

    assert session.paper_by_id("10.1000/dup") is None
    assert session.paper_by_id("10.1000/missing") is None
