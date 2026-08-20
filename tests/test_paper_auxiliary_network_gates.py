"""Persistent capability gates must stop secondary official-API calls too."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.models import Paper


class ExplodingClient:
    async def request(self, *_args, **_kwargs):
        raise AssertionError("network request must not be created")

    async def get(self, *_args, **_kwargs):
        raise AssertionError("network request must not be created")


def test_integrity_crossref_and_arxiv_gates_prevent_network(monkeypatch):
    from tools.search import integrity

    monkeypatch.setattr(
        integrity, "_capability_allowed", lambda source: source not in {"crossref", "arxiv"}
    )
    client = ExplodingClient()
    assert asyncio.run(integrity._crossref_lookup(client, "10.1/example")) is None
    assert asyncio.run(integrity._arxiv_journal_ref(client, "1706.03762")) == ""


def test_openalex_reference_paths_stop_before_client_creation(monkeypatch):
    from tools.search import openalex_refs

    monkeypatch.setattr(openalex_refs, "_capability_allowed", lambda: False)
    monkeypatch.setattr(
        openalex_refs, "get_search_http_client",
        lambda: (_ for _ in ()).throw(AssertionError("client must not be created")),
    )
    assert asyncio.run(openalex_refs.fetch_referenced_works(["10.1/a"])) == {}
    assert asyncio.run(openalex_refs.resolve_openalex_ids(["10.1/a"])) == {}
    assert asyncio.run(openalex_refs.fetch_works_metadata(["W1"])) == []
    assert asyncio.run(openalex_refs.fetch_citation_bundle(["10.1/a"])) == ({}, {}, "no_doi")


def test_openalex_census_gate_stops_all_groupby_calls(monkeypatch):
    from tools.search import openalex_census

    monkeypatch.setattr(
        "core.paper_search_settings_store.source_capability_enabled",
        lambda _source, _capability: (False, "disabled"),
    )
    monkeypatch.setattr(
        openalex_census, "get_settings",
        lambda: SimpleNamespace(search=SimpleNamespace(openalex_api_key="key")),
    )
    monkeypatch.setattr(
        openalex_census, "get_search_http_client",
        lambda: (_ for _ in ()).throw(AssertionError("client must not be created")),
    )
    assert asyncio.run(openalex_census.fetch_census("graph learning")) == {
        "yearly": [], "top_authors": [], "top_institutions": [], "top_venues": [],
    }


def test_crossref_export_enrichment_gate_stops_http_client(monkeypatch):
    from tools.export import enrich

    monkeypatch.setattr(
        "core.paper_search_settings_store.source_capability_enabled",
        lambda _source, _capability: (False, "disabled"),
    )
    monkeypatch.setattr(
        enrich.httpx, "AsyncClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP client must not be constructed")
        ),
    )
    assert asyncio.run(enrich.enrich_by_dois(["10.1/example"])) == {}


def test_integrity_sweep_with_all_lookup_sources_disabled_is_offline(monkeypatch):
    from tools.search import integrity

    monkeypatch.setattr(integrity, "_capability_allowed", lambda _source: False)
    monkeypatch.setattr(integrity, "get_search_http_client", lambda: ExplodingClient())
    papers = [Paper(
        id="P", title="Paper", source="arxiv", doi="10.1/example",
        urls={"arxiv": "https://arxiv.org/abs/1706.03762"},
    )]
    result = asyncio.run(integrity.sweep(papers))
    assert result["report"][0]["status"] == "preprint_published"
