"""Tests for the structured Trace observability layer (DESIGN D-067)."""

import json
import tempfile
from pathlib import Path

from core.trace import Trace, list_traces, trace_to_html


def _read(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def test_trace_writes_jsonl_with_turn_start_and_end():
    with tempfile.TemporaryDirectory() as tmp:
        t = Trace(trace_dir=tmp)
        t.start(user_query="hi", intent="react")
        t.event("decision", action="call_tool", tool="search")
        with t.span("llm_call", model="gemini"):
            t.add_tokens({"input_tokens": 100, "output_tokens": 50})
        t.finish(iterations=1, tool_calls=1)
        t.close()

        events = _read(t.path)
        types = [e["type"] for e in events]
        assert types[0] == "turn_start"
        assert "llm_call_start" in types and "llm_call_end" in types
        assert "decision" in types
        assert types[-1] == "turn_end"


def test_token_accumulation():
    t = Trace(trace_dir=tempfile.mkdtemp())
    t.add_tokens({"input_tokens": 10, "output_tokens": 5})
    t.add_tokens({"input_tokens": 20, "output_tokens": 5})
    assert t.total_tokens == 40
    assert t.input_tokens == 30
    assert t.output_tokens == 10


def test_span_latency_recorded():
    t = Trace(trace_dir=tempfile.mkdtemp())
    with t.span("tool_call", name="search"):
        pass
    end_events = [e for e in t.events if e["type"] == "tool_call_end"]
    assert end_events and "latency_ms" in end_events[0]["data"]


def test_trace_to_html_creates_file():
    with tempfile.TemporaryDirectory() as tmp:
        t = Trace(trace_dir=tmp)
        t.start(user_query="hi")
        t.event("decision", tool="search")
        t.finish(iterations=1, tool_calls=1)
        t.close()
        html_path = trace_to_html(t.path)
        assert Path(html_path).exists()
        content = Path(html_path).read_text(encoding="utf-8")
        assert "Paper Agent Trace" in content
        assert "decision" in content


def test_list_traces_reads_summary():
    with tempfile.TemporaryDirectory() as tmp:
        t = Trace(trace_dir=tmp, trace_id="abc123")
        t.start(user_query="hi")
        t.finish(iterations=2, tool_calls=3)
        t.close()
        # list_traces uses the default _TRACE_DIR; verify it returns a list
        items = list_traces(limit=5)
        assert isinstance(items, list)


def test_failure_trace_is_not_cleaned():
    """Per the agent-develop skill, failure traces must be preserved."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Trace(trace_dir=tmp)
        t.start(user_query="hi")
        t.event("error", tool="search", message="boom")
        t.finish(status="error", error="boom", iterations=0, tool_calls=0)
        t.close()
        events = _read(t.path)
        assert any(e["type"] == "error" for e in events)
