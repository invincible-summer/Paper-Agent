"""Offline parsing tests for the HAL / OpenAIRE / CORE search backends.

Each backend exposes a pure parse function; these tests feed sample payloads
— no HTTP. Also covers CORE's no-key auto-disable.
"""
from __future__ import annotations

import asyncio
import types


def test_hal_parse():
    from tools.search.hal import parse_docs

    data = {"response": {"docs": [
        {"halId_s": "hal-0001", "title_s": ["Sociology Paper"],
         "abstract_s": ["an abstract"], "authFullName_s": ["Alice A", "Bob B"],
         "producedDateY_i": 2021, "doiId_s": "10.1/x",
         "journalTitle_s": "Revue", "fileMain_s": "https://hal.science/hal-0001/document",
         "keyword_s": ["sociology"]},
        {"title_s": []},  # no title -> skipped
    ]}}
    papers = parse_docs(data)
    assert len(papers) == 1
    p = papers[0]
    assert p.id == "doi:10.1/x"
    assert p.title == "Sociology Paper"
    assert p.authors == ["Alice A", "Bob B"]
    assert p.year == 2021
    assert p.venue == "Revue"
    assert p.pdf_url == "https://hal.science/hal-0001/document"
    assert p.urls["hal"] == "https://hal.science/hal-0001"
    assert p.source == "hal"


def test_hal_parse_scalar_fields():
    """HAL sometimes returns scalars instead of single-element lists."""
    from tools.search.hal import parse_docs

    data = {"response": {"docs": [
        {"halId_s": "hal-9", "title_s": "Scalar Title", "producedDateY_i": "2018"},
    ]}}
    papers = parse_docs(data)
    assert len(papers) == 1
    assert papers[0].title == "Scalar Title"
    assert papers[0].year == 2018


def test_openaire_parse_open_access_only():
    from tools.search.openaire import parse_results

    data = {"response": {"results": {"result": [
        {"metadata": {"oaf:entity": {"oaf:result": {
            "title": {"$": "OA Paper"},
            "dateofacceptance": {"$": "2020-03-01"},
            "description": {"$": "abstract text"},
            "creator": [{"$": "Alice A"}, {"$": "Bob B"}],
            "pid": [{"@classid": "doi", "$": "10.2/y"}],
            "journal": {"title": {"$": "Journal J"}},
            "children": {"instance": [
                {"accessright": {"@classid": "closed"},
                 "webresource": {"url": {"$": "https://closed.example/x.pdf"}}},
                {"accessright": {"@classid": "open", "@classname": "Open Access"},
                 "webresource": {"url": {"$": "https://oa.example/x.pdf"}}},
            ]},
        }}}},
        # closed-access only -> pdf_url must be None, never the closed link
        {"metadata": {"oaf:entity": {"oaf:result": {
            "title": {"$": "Closed Paper"},
            "children": {"instance": [
                {"accessright": {"@classid": "restricted"},
                 "webresource": {"url": {"$": "https://pub.example/paywalled.pdf"}}},
            ]},
        }}}},
    ]}}}
    papers = parse_results(data)
    assert len(papers) == 2
    oa, closed = papers
    assert oa.id == "doi:10.2/y"
    assert oa.year == 2020
    assert oa.authors == ["Alice A", "Bob B"]
    assert oa.venue == "Journal J"
    assert oa.pdf_url == "https://oa.example/x.pdf"
    assert closed.pdf_url is None


def test_core_parse():
    from tools.search.core import parse_results

    data = {"results": [
        {"id": 42, "title": "Repo Paper", "authors": [{"name": "Alice A"}],
         "yearPublished": 2019, "doi": "10.3/z",
         "downloadUrl": "https://core.ac.uk/download/42.pdf",
         "citationCount": 5, "abstract": "abs", "publisher": "Uni Press"},
    ]}
    papers = parse_results(data)
    assert len(papers) == 1
    p = papers[0]
    assert p.id == "doi:10.3/z"
    assert p.year == 2019
    assert p.citation_count == 5
    assert p.pdf_url == "https://core.ac.uk/download/42.pdf"
    assert p.urls["core"] == "https://core.ac.uk/works/42"
    assert p.source == "core"


def test_core_disabled_without_key(monkeypatch):
    import tools.search.core as core_mod

    fake = types.SimpleNamespace(search=types.SimpleNamespace(core_api_key=""))
    monkeypatch.setattr(core_mod, "get_settings", lambda: fake)
    assert asyncio.run(core_mod.CoreBackend().search("graph neural networks")) == []


def test_new_sources_registered_in_manager():
    from tools.search.manager import BACKENDS

    for name in ("hal", "openaire", "core"):
        assert name in BACKENDS
