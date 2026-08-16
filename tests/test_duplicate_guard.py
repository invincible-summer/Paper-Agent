"""Tests for the duplicate-call guard key (DESIGN D-068).

The guard lives inside chat_turn; the identity logic is extracted as
make_call_key so it is unit-testable without an LLM.
"""

from agents.chat_agent import make_call_key


def test_order_independent_args():
    a = make_call_key("search", {"topic": "x", "language": "en"})
    b = make_call_key("search", {"language": "en", "topic": "x"})
    assert a == b


def test_different_args_differ():
    a = make_call_key("search", {"topic": "x"})
    b = make_call_key("search", {"topic": "y"})
    assert a != b


def test_different_tool_differ():
    assert make_call_key("graph", {}) != make_call_key("review", {})


def test_set_dedup_semantics():
    """The guard rejects the second identical call in a turn."""
    seen = set()
    key = make_call_key("search", {"topic": "x"})
    assert key not in seen
    seen.add(key)
    assert key in seen  # second identical call would be rejected
