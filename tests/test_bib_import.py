"""bib_import: BibTeX parser + import-to-candidates tool (no real API)."""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.models import Paper
from core.tool_protocol import ErrorCode
from agents.session import ChatSession
from agents.tools_impl import _tool_bib_import, validate_args
from tools.export.bibtex_import import parse_bibtex

UPLOADS = pathlib.Path(__file__).resolve().parents[1] / "data" / "uploads"

SAMPLE_BIB = r"""@article{vaswani2017,
  title = {Attention Is All You Need},
  author = {Vaswani, Ashish and Shazeer, Noam},
  journal = {Nature},
  year = {2017},
  doi = {10.1/attention},
  abstract = {We propose a new architecture based on attention.}
}

@inproceedings{jones2019,
  title = {On the {GPU} {Scaling} of Methods},
  author = {Jones, Bob and Lee, Ann},
  booktitle = {Proc. of NeurIPS},
  year = {2019}
}

@string{foo = "bar"}
@comment{this should be skipped}
"""


# ---------------------------------------------------------------------------
# parse_bibtex pure function
# ---------------------------------------------------------------------------

def test_parse_two_entries_skips_metadata():
    entries = parse_bibtex(SAMPLE_BIB)
    assert len(entries) == 2
    keys = [e["key"] for e in entries]
    assert keys == ["vaswani2017", "jones2019"]


def test_parse_article_fields():
    e = parse_bibtex(SAMPLE_BIB)[0]
    assert e["title"] == "Attention Is All You Need"
    assert e["doi"] == "10.1/attention"
    assert e["venue"] == "Nature"
    assert e["year"] == 2017
    assert e["entry_type"] == "article"
    assert "new architecture" in e["abstract"]


def test_parse_authors_split_by_and():
    e = parse_bibtex(SAMPLE_BIB)[0]
    assert e["authors"] == ["Vaswani, Ashish", "Shazeer, Noam"]


def test_parse_nested_braces_collapsed_and_booktitle_venue():
    e = parse_bibtex(SAMPLE_BIB)[1]
    assert e["title"] == "On the GPU Scaling of Methods"
    assert e["venue"] == "Proc. of NeurIPS"
    assert e["entry_type"] == "inproceedings"


def test_parse_quoted_values():
    entries = parse_bibtex('@misc{x, title = "Hello World", year = {2020}}')
    assert entries[0]["title"] == "Hello World"
    assert entries[0]["year"] == 2020


def test_parse_empty_and_malformed():
    assert parse_bibtex("") == []
    assert parse_bibtex("not a bibtex file at all") == []
    # unbalanced brace → that entry skipped, others kept
    entries = parse_bibtex("@article{a, title = {Ok}}\n@misc{bad, title = {oops")
    assert len(entries) == 1
    assert entries[0]["title"] == "Ok"


def test_parse_missing_fields_safe():
    e = parse_bibtex("@misc{onlykey}")[0]
    assert e["key"] == "onlykey"
    assert e["title"] == "" and e["year"] is None and e["authors"] == []


# ---------------------------------------------------------------------------
# _tool_bib_import end-to-end (stubbed enrich, temp upload)
# ---------------------------------------------------------------------------

def _write_bib(aid: str, content: str):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    (UPLOADS / f"{aid}.txt").write_text(content, encoding="utf-8")


def _remove(aid: str):
    (UPLOADS / f"{aid}.txt").unlink(missing_ok=True)


def test_tool_guard_no_attachment():
    res = asyncio.run(_tool_bib_import({}, ChatSession(), None))
    assert res.is_error and res.error_code == ErrorCode.NO_PAPERS


def test_tool_imports_into_candidates(monkeypatch):
    aid = "bib-test-1"
    _write_bib(aid, SAMPLE_BIB)
    try:
        async def fake_enrich(dois):
            return {"10.1/attention": {"venue": "Nature", "volume": "41"}}
        monkeypatch.setattr("tools.export.enrich.enrich_by_dois", fake_enrich)

        s = ChatSession()
        s.attachments = [{"id": aid, "filename": "lib.bib"}]
        res = asyncio.run(_tool_bib_import({}, s, None))
        assert not res.is_error
        assert len(s.candidates) == 2
        assert res.data["report"]["imported"] == 2
        assert res.data["report"]["enriched"] == 1
        titles = {c.title for c in s.candidates}
        assert "Attention Is All You Need" in titles
    finally:
        _remove(aid)


def test_tool_dedups_against_existing_corpus(monkeypatch):
    aid = "bib-test-2"
    _write_bib(aid, SAMPLE_BIB)
    try:
        async def fake_enrich(dois):
            return {}
        monkeypatch.setattr("tools.export.enrich.enrich_by_dois", fake_enrich)

        s = ChatSession()
        # Pre-seed a paper whose title collides with vaswani2017.
        s.papers = [Paper(id="doi:10.1/attention", title="Attention Is All You Need",
                          doi="10.1/attention", source="openalex")]
        s.attachments = [{"id": aid, "filename": "lib.bib"}]
        res = asyncio.run(_tool_bib_import({}, s, None))
        assert res.data["report"]["imported"] == 1  # vaswani deduped, jones imported
        assert res.data["report"]["failed"] == 1
        assert len(s.candidates) == 1
    finally:
        _remove(aid)


def test_schema_validation():
    args, err_res = validate_args("bib_import", {"attachment": "lib"})
    assert err_res is None and args["attachment"] == "lib"
    _, err_res = validate_args("bib_import", {"attachment": 123})
    assert err_res is not None and err_res.error_code == ErrorCode.VALIDATION_ERROR


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
