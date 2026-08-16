"""integrity_sweep: deterministic retraction / erratum / preprint→published checks.

Pure-function parsers + a stubbed end-to-end sweep (no real API calls) + the
tool-impl guard logic.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.models import Paper
from core.tool_protocol import ErrorCode
from agents.session import ChatSession
from agents.tools_impl import _tool_integrity_sweep, validate_args
from tools.search import integrity


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------

def test_parse_openalex_integrity_flags():
    assert integrity.parse_openalex_integrity({"is_retracted": True})["is_retracted"] is True
    assert integrity.parse_openalex_integrity({})["is_retracted"] is False
    merged = integrity.parse_openalex_integrity({"merged_into": {"id": "W123"}})
    assert merged["merged_into"] == "W123"


def test_parse_crossref_relations_clean():
    ci = integrity.parse_crossref_relations({})
    assert ci["concerns"] == [] and ci["is_retraction"] is False


def test_parse_crossref_relations_correction_and_concern():
    msg = {"relation": {
        "has-correction": [{"id": "https://doi.org/10.1/corr"}],
        "has-expression-of-concern": [{"id": "https://doi.org/10.1/ec"}],
    }}
    ci = integrity.parse_crossref_relations(msg)
    assert ci["is_retraction"] is False
    assert len(ci["concerns"]) == 2
    assert any("has-correction" in c for c in ci["concerns"])


def test_parse_crossref_relations_retraction_via_relation_and_assertion():
    msg = {"relation": {"has-retraction": [{"id": "https://doi.org/10.1/ret"}]}}
    assert integrity.parse_crossref_relations(msg)["is_retraction"] is True
    msg2 = {"assertion": [{"label": "Retraction", "value": "This article is retracted."}]}
    assert integrity.parse_crossref_relations(msg2)["is_retraction"] is True


def test_arxiv_id_extraction():
    p = Paper(source="arxiv", urls={"arxiv": "http://arxiv.org/abs/2401.12345v1"})
    assert integrity._arxiv_id(p) == "2401.12345v1"
    p2 = Paper(source="arxiv", urls={"arxiv": "http://arxiv.org/pdf/2401.99999"})
    assert integrity._arxiv_id(p2) == "2401.99999"
    assert integrity._arxiv_id(Paper()) == ""


def test_group_facts_buckets():
    report = [
        {"paper_id": "a", "status": "retracted"},
        {"paper_id": "b", "status": "clean"},
        {"paper_id": "c", "status": "clean"},
    ]
    facts = integrity._group_facts(report)
    assert facts["retracted"] == ["a"]
    assert sorted(facts["clean"]) == ["b", "c"]
    assert "concern" not in facts  # empty buckets dropped


# ---------------------------------------------------------------------------
# End-to-end sweep with stubbed lookups (no network)
# ---------------------------------------------------------------------------

def test_sweep_classifies_each_status(monkeypatch):
    async def fake_openalex(client, doi):
        return {"is_retracted": doi == "10.1/retracted"}

    async def fake_crossref(client, doi):
        if doi == "10.1/retracted":
            return {"relation": {"has-retraction": [{"id": "https://doi.org/10.1/ret"}]}}
        if doi == "10.1/corrected":
            return {"relation": {"has-correction": [{"id": "https://doi.org/10.1/corr"}]}}
        return {}  # clean for the clean DOI

    async def fake_arxiv(client, aid):
        return "Nature, 2024" if "2401.12345" in aid else ""

    monkeypatch.setattr(integrity, "_openalex_lookup", fake_openalex)
    monkeypatch.setattr(integrity, "_crossref_lookup", fake_crossref)
    monkeypatch.setattr(integrity, "_arxiv_journal_ref", fake_arxiv)

    papers = [
        Paper(id="p1", title="Retracted one", doi="10.1/retracted", source="openalex"),
        Paper(id="p2", title="Corrected one", doi="10.1/corrected", source="crossref"),
        Paper(id="p3", title="arXiv preprint", source="arxiv",
              urls={"arxiv": "http://arxiv.org/abs/2401.12345v1"}),
        Paper(id="p4", title="Clean one", doi="10.1/clean", source="openalex"),
        Paper(id="p5", title="No DOI non-arxiv", source="doaj"),
    ]
    result = asyncio.run(integrity.sweep(papers))
    by_id = {r["paper_id"]: r["status"] for r in result["report"]}
    assert by_id == {
        "p1": "retracted", "p2": "concern", "p3": "preprint_published",
        "p4": "clean", "p5": "unknown",
    }
    facts = result["facts"]
    assert "p1" in facts["retracted"] and "p4" in facts["clean"]


# ---------------------------------------------------------------------------
# Tool-impl guards
# ---------------------------------------------------------------------------

def _sess_with_papers():
    s = ChatSession()
    s.papers = [Paper(id="p1", title="T", doi="10.1/x", source="openalex")]
    return s


def test_tool_guard_no_papers():
    s = ChatSession()  # no papers, no candidates
    res = asyncio.run(_tool_integrity_sweep({}, s, None))
    assert res.is_error and res.error_code == ErrorCode.NO_PAPERS


def test_tool_guard_bad_paper_ids():
    res = asyncio.run(_tool_integrity_sweep({"paper_ids": ["ghost"]}, _sess_with_papers(), None))
    assert res.is_error and res.error_code == ErrorCode.VALIDATION_ERROR


def test_tool_success_path(monkeypatch):
    async def fake_sweep(papers):
        return {"report": [{"paper_id": "p1", "status": "clean", "note": "✅ ok"}],
                "facts": {"clean": ["p1"]}}
    monkeypatch.setattr(integrity, "sweep", fake_sweep)
    res = asyncio.run(_tool_integrity_sweep({}, _sess_with_papers(), None))
    assert not res.is_error
    assert res.data["facts"] == {"clean": ["p1"]}
    assert "无异常 1" in res.text


def test_schema_validation():
    args, err_res = validate_args("integrity_sweep", {"paper_ids": ["p1"]})
    assert err_res is None and args["paper_ids"] == ["p1"]
    # bad type
    _, err_res = validate_args("integrity_sweep", {"paper_ids": "not-a-list"})
    assert err_res is not None
    assert err_res.error_code == ErrorCode.VALIDATION_ERROR


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
