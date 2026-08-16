"""field_census: OpenAlex group_by aggregation + portrait (no real API)."""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.tool_protocol import ErrorCode
from agents.session import ChatSession
from agents.tools_impl import _tool_field_census, validate_args
from tools.search import openalex_census


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------

def test_parse_groupby_normalizes():
    groups = [
        {"key": "W1", "key_display_name": "Jiawei Han", "count": 42},
        {"key": "2018", "key_display_name": "2018", "count": 100},
        {"key": "", "key_display_name": "", "count": 5},  # dropped
    ]
    out = openalex_census.parse_groupby(groups)
    assert len(out) == 2
    assert out[0] == {"key": "W1", "name": "Jiawei Han", "count": 42}


def test_parse_groupby_falls_back_to_key():
    out = openalex_census.parse_groupby([{"key": "anon", "count": 3}])
    assert out == [{"key": "anon", "name": "anon", "count": 3}]


def test_sort_yearly_ascending_filters_and_sorts():
    rows = [
        {"key": "2020", "name": "2020", "count": 50},
        {"key": "null", "name": "Unknown", "count": 9},  # non-year dropped
        {"key": "2018", "name": "2018", "count": 30},
    ]
    out = openalex_census.sort_yearly_ascending(rows)
    assert [r["key"] for r in out] == ["2018", "2020"]


# ---------------------------------------------------------------------------
# _tool_field_census (stubbed aggregation + portrait)
# ---------------------------------------------------------------------------

def _sess():
    s = ChatSession()
    s.topic = "graph neural networks"
    s.search_queries = ["graph neural networks"]
    return s


def test_tool_guard_no_topic():
    res = asyncio.run(_tool_field_census({}, ChatSession(), None))
    assert res.is_error and res.error_code == ErrorCode.NO_PAPERS


def test_tool_success(monkeypatch):
    async def fake_fetch(query):
        return {
            "yearly": [{"key": "2020", "name": "2020", "count": 100},
                       {"key": "2021", "name": "2021", "count": 120}],
            "top_authors": [{"key": "W1", "name": "Han", "count": 10}],
            "top_institutions": [],
            "top_venues": [{"key": "S1", "name": "NeurIPS", "count": 30}],
        }

    async def fake_portrait(query, census):
        return "该领域快速增长。"

    monkeypatch.setattr(openalex_census, "fetch_census", fake_fetch)
    monkeypatch.setattr(openalex_census, "portrait", fake_portrait)

    res = asyncio.run(_tool_field_census({}, _sess(), None))
    assert not res.is_error
    assert len(res.data["yearly"]) == 2
    assert res.data["portrait"] == "该领域快速增长。"
    assert res.data["top_venues"][0]["name"] == "NeurIPS"


def test_tool_partial_when_empty(monkeypatch):
    async def fake_fetch(query):
        return {"yearly": [], "top_authors": [], "top_institutions": [], "top_venues": []}

    async def fake_portrait(query, census):
        return ""

    monkeypatch.setattr(openalex_census, "fetch_census", fake_fetch)
    monkeypatch.setattr(openalex_census, "portrait", fake_portrait)

    res = asyncio.run(_tool_field_census({}, _sess(), None))
    assert res.status == "partial"


def test_schema_validation_no_required_args():
    args, err_res = validate_args("field_census", {})
    assert err_res is None
    assert args == {}


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
