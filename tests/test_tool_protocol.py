"""Tests for the unified tool response protocol (DESIGN D-067)."""

from core.tool_protocol import ErrorCode, ToolResult, err, ok, partial_result


def test_success_no_error_key_and_flattens_data():
    r = ok("search", "found 3", total_papers=3, core_titles=["A", "B"])
    d = r.to_dict()
    assert d["status"] == "success"
    assert d["tool"] == "search"
    assert d["summary"] == "found 3"
    assert d["text"] == "found 3"
    assert d["total_papers"] == 3
    assert d["core_titles"] == ["A", "B"]
    assert "error" not in d


def test_error_carries_code_and_message():
    r = err("graph", ErrorCode.NO_PAPERS, "No papers found.")
    assert r.is_error
    assert r.error_code == "NO_PAPERS"
    d = r.to_dict()
    assert d["status"] == "error"
    assert d["error"] == {"code": "NO_PAPERS", "message": "No papers found."}


def test_partial_status():
    r = partial_result("search", "partial", total_papers=1)
    d = r.to_dict()
    assert d["status"] == "partial"
    assert "error" not in d
    assert d["total_papers"] == 1


def test_error_code_property_none_for_success():
    assert ok("x", "y").error_code is None
    assert ok("x", "y").is_error is False


def test_stats_optional():
    r = ToolResult("success", tool="search", text="ok")
    assert "stats" not in r.to_dict()
