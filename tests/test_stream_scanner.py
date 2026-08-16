"""Tests for the streaming tag scanner state machine.

The scanner holds back up to len("</thinking>")=11 chars to avoid emitting a
partial tag as plain text. Short remainders therefore stay buffered until more
input arrives (or until the stream-end flush). Tags: <thinking> / </thinking>;
states: pre_thinking -> in_thinking -> in_answer.
"""

from agents.orchestrator import stream_scan

HOLD = 11  # max tag length; safe text is only emitted once it exceeds this margin


def test_pre_thinking_emits_answer_until_open_tag():
    # "Hello" (5) < hold, but a *complete* tag follows so the pre-tag text is
    # unambiguously safe and emitted.
    buf, state, emits = stream_scan("Hello<thinking>", "pre_thinking")
    assert state == "in_thinking"
    assert emits == [("answer", "Hello")]
    assert buf == ""


def test_in_thinking_emits_thinking_until_close():
    buf, state, emits = stream_scan("reasoning here</thinking>", "in_thinking")
    assert state == "in_answer"
    assert emits == [("thinking", "reasoning here")]
    assert buf == ""


def test_answer_after_close_streams():
    text = "final answer text here"
    buf, state, emits = stream_scan(text, "in_answer")
    assert state == "in_answer"
    # safe text = all but the last HOLD-1 chars (held as potential partial tag)
    assert emits == [("answer", text[:len(text) - HOLD + 1])]
    assert buf == text[len(text) - HOLD + 1:]


def test_partial_tag_prefix_is_held_back():
    # "</th" could become "</thinking>" — it must stay buffered, never emitted.
    buf, state, emits = stream_scan("some longer text</th", "in_answer")
    assert state == "in_answer"
    emitted = "".join(c for _, c in emits)
    assert "</th" not in emitted
    assert buf.endswith("</th")


def test_multiple_tags_in_one_chunk():
    buf, state, emits = stream_scan(
        "a<thinking>b</thinking>c" + "x" * 20, "pre_thinking")
    assert state == "in_answer"
    assert ("answer", "a") in emits
    assert ("thinking", "b") in emits
    # trailing answer text emitted except the held-back tail
    assert any(e[0] == "answer" and e[1].startswith("c") for e in emits)


def test_state_transitions_complete_cycle():
    state = "pre_thinking"
    buf = ""
    all_emits = []
    for piece in ["<thinking>think", "ing</thinking>ans", "wer" + "z" * 20]:
        buf += piece
        buf, state, emits = stream_scan(buf, state)
        all_emits.extend(emits)
    assert state == "in_answer"
    types = [e[0] for e in all_emits]
    assert "thinking" in types and "answer" in types
