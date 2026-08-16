"""build_citation_edges: edges come only from real OpenAlex referenced_works."""
from core.models import Paper
from tools.search.openalex_refs import build_citation_edges


def _papers():
    return [
        Paper(id="A", doi="10.1/a"),
        Paper(id="B", doi="10.1/b"),
        Paper(id="C", doi="10.1/c"),
        Paper(id="D"),  # no DOI -> never participates
    ]


def test_edges_only_from_referenced_works():
    refs_by_doi = {"10.1/b": ["W1", "W9"], "10.1/c": ["W2"]}
    doi_to_openalex = {"10.1/a": "W1", "10.1/b": "W2", "10.1/c": "W3"}
    edges = build_citation_edges(_papers(), refs_by_doi, doi_to_openalex)
    # B cites A (W1 in B's refs); C cites B. W9 is out-of-library -> dropped.
    assert edges == [("B", "A"), ("C", "B")]


def test_no_doi_no_edges():
    papers = [Paper(id="X"), Paper(id="Y")]
    assert build_citation_edges(papers, {}, {}) == []


def test_self_loop_and_duplicates_removed():
    papers = [Paper(id="A", doi="10.1/a")]
    refs_by_doi = {"10.1/a": ["W1", "W1"]}
    doi_to_openalex = {"10.1/a": "W1"}
    assert build_citation_edges(papers, refs_by_doi, doi_to_openalex) == []


def test_direction_is_citer_to_cited():
    papers = [Paper(id="OLD", doi="10.1/old"), Paper(id="NEW", doi="10.1/new")]
    refs_by_doi = {"10.1/new": ["Wold"]}  # NEW's reference list contains OLD
    doi_to_openalex = {"10.1/old": "Wold", "10.1/new": "Wnew"}
    assert build_citation_edges(papers, refs_by_doi, doi_to_openalex) == [("NEW", "OLD")]
