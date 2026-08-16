"""Tests for history source derivation (D-073, fixed D-079).

The original bug: a chat record that merely searched (papers present, but no
structured pipeline outputs) was labeled 'structured'/'both', so it appeared
as structured work on the structured page. D-079 fixes this by basing
has_structured on actual pipeline outputs (summaries/review/graph/framework),
not on raw search results.
"""
from core.history_store import _derive_source


def test_search_only_chat_is_chat():
    """The bug: chat with search results must NOT count as structured work."""
    assert _derive_source({
        "messages": [{"role": "user", "content": "search X"}],
        "papers": [{"id": "1"}, {"id": "2"}],
    }) == "chat"


def test_chat_with_deep_read_is_both():
    assert _derive_source({
        "messages": [{"role": "user", "content": "hi"}],
        "paper_summaries": {"1": {"research_problem": "x"}},
    }) == "both"


def test_chat_with_graph_is_both():
    assert _derive_source({
        "messages": [{"role": "user", "content": "hi"}],
        "graph_data": {"n_nodes": 5},
    }) == "both"


def test_pure_structured_search_is_structured():
    assert _derive_source({"papers": [{"id": "1"}]}) == "structured"


def test_structured_with_review_is_structured():
    assert _derive_source({"literature_review": "a long review"}) == "structured"


def test_empty_is_chat():
    assert _derive_source({}) == "chat"
