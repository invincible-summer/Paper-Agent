from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.models import Paper
from tools.search import diagnostics
from tools.search.base import SearchOutcome


def _ready(monkeypatch):
    monkeypatch.setattr(diagnostics, "_configuration_status", lambda _source: (True, "ready"))


def test_full_diagnostic_separates_connectivity_search_abstract_and_fulltext(monkeypatch):
    _ready(monkeypatch)
    paper = Paper(id="P", title="Paper", source="arxiv", abstract="useful abstract",
                  pdf_url="https://arxiv.org/pdf/1706.03762.pdf")
    async def search(_source):
        return SearchOutcome("arxiv", [paper], "ok", http_status=200,
                             request_count=1, network_ms=320, final_domain="export.arxiv.org"), [paper]
    async def speed(target, url):
        assert target == "arxiv" and url.endswith(".pdf")
        return {"status": "ok", "elapsed_ms": 1000, "bytes_read": 1024 * 1024,
                "kb_per_second": 1024.0, "pdf_magic_valid": True,
                "error_code": None, "message": None}
    monkeypatch.setattr(diagnostics, "_official_search", search)
    monkeypatch.setattr(diagnostics, "_speed_test_url", speed)
    row = asyncio.run(diagnostics.diagnose_platform("arxiv"))
    assert row["connectivity"]["status"] == "ok"
    assert row["search"]["result_count"] == 1
    assert row["abstract"]["abstract_length"] == len("useful abstract")
    assert row["fulltext"]["pdf_magic_valid"] is True
    assert row["auto_disabled_capabilities"] == []


def test_connectivity_hard_failure_closes_all_supported_capabilities(monkeypatch):
    _ready(monkeypatch)
    async def search(_source):
        return SearchOutcome("openaire", [], "connection_error", error_code="connection_error", network_ms=123), []
    monkeypatch.setattr(diagnostics, "_official_search", search)
    row = asyncio.run(diagnostics.diagnose_platform("openaire"))
    assert row["connectivity"]["status"] == "failed"
    assert set(row["auto_disabled_capabilities"]) == {"search", "abstract", "fulltext"}



def test_single_capability_probe_still_closes_all_on_base_connectivity_failure(monkeypatch):
    _ready(monkeypatch)

    async def search(_source):
        return SearchOutcome(
            "openaire", [], "connection_error",
            error_code="connection_error", network_ms=50,
        ), []

    monkeypatch.setattr(diagnostics, "_official_search", search)
    row = asyncio.run(diagnostics.diagnose_platform("openaire", {"search"}))
    assert row["connectivity"]["status"] == "failed"
    assert set(row["auto_disabled_capabilities"]) == {
        "search", "abstract", "fulltext",
    }
    assert set(row["_persist_capabilities"]) == {
        "search", "abstract", "fulltext",
    }

def test_search_empty_closes_only_search_when_dependent_probes_cannot_run(monkeypatch):
    _ready(monkeypatch)
    async def search(_source):
        return SearchOutcome("crossref", [], "reachable_empty", http_status=200, network_ms=20), []
    monkeypatch.setattr(diagnostics, "_official_search", search)
    row = asyncio.run(diagnostics.diagnose_platform("crossref"))
    assert row["connectivity"]["status"] == "ok"
    assert row["search"]["status"] == "failed"
    assert row["abstract"]["status"] == "failed"
    assert row["abstract"]["error_code"] == "search_prerequisite_failed"
    assert row["auto_disabled_capabilities"] == ["search"]
    assert row["fulltext"]["status"] == "not_applicable"



def test_abstract_empty_closes_only_abstract_when_search_succeeds(monkeypatch):
    _ready(monkeypatch)
    paper = Paper(id="P", title="Paper", source="crossref", abstract="")

    async def search(_source):
        return SearchOutcome(
            "crossref", [paper], "ok", http_status=200, network_ms=20
        ), [paper]

    monkeypatch.setattr(diagnostics, "_official_search", search)
    row = asyncio.run(diagnostics.diagnose_platform("crossref"))
    assert row["connectivity"]["status"] == "ok"
    assert row["search"]["status"] == "ok"
    assert row["abstract"]["error_code"] == "abstract_empty"
    assert row["auto_disabled_capabilities"] == ["abstract"]


def test_search_prerequisite_failure_does_not_close_fulltext_independently(monkeypatch):
    _ready(monkeypatch)

    async def search(_source):
        return SearchOutcome(
            "openaire", [], "reachable_empty", http_status=200, network_ms=20
        ), []

    monkeypatch.setattr(diagnostics, "_official_search", search)
    row = asyncio.run(diagnostics.diagnose_platform("openaire"))
    assert row["fulltext"]["error_code"] == "search_prerequisite_failed"
    assert row["auto_disabled_capabilities"] == ["search"]


def test_local_anonymous_budget_exhaustion_is_not_network_failure(monkeypatch):
    _ready(monkeypatch)

    async def search(_source):
        return SearchOutcome(
            "openaire", [], "local_budget_exhausted",
            error_code="local_budget_exhausted", network_ms=0,
        ), []

    monkeypatch.setattr(diagnostics, "_official_search", search)
    row = asyncio.run(diagnostics.diagnose_platform("openaire"))
    assert row["connectivity"]["status"] == "not_configured"
    assert row["search"]["status"] == "not_configured"
    assert row["auto_disabled_capabilities"] == []

def test_not_configured_never_auto_disables(monkeypatch):
    monkeypatch.setattr(diagnostics, "_configuration_status", lambda _source: (False, "missing_api_key"))
    row = asyncio.run(diagnostics.diagnose_platform("openalex"))
    assert row["search"]["status"] == "not_configured"
    assert row["auto_disabled_capabilities"] == []



def test_pdf_candidate_prefers_repository_copy_from_official_results():
    papers = [
        Paper(id="bad", pdf_url="https://publisher.example/blocked.pdf"),
        Paper(id="repo", pdf_url="https://europepmc.org/articles/PMC1?pdf=render"),
    ]
    assert diagnostics._pdf_candidate("europepmc", papers) == papers[1].pdf_url

    openalex = [
        Paper(id="publisher", pdf_url="https://publisher.example/paper.pdf"),
        Paper(id="arxiv", pdf_url="https://arxiv.org/pdf/2102.05095"),
    ]
    assert diagnostics._pdf_candidate("openalex", openalex) == openalex[1].pdf_url

def test_slow_valid_pdf_warns_without_auto_disable(monkeypatch):
    _ready(monkeypatch)
    paper = Paper(id="P", title="Paper", source="arxiv", abstract="a",
                  pdf_url="https://arxiv.org/pdf/1706.03762.pdf")
    async def search(_source):
        return SearchOutcome("arxiv", [paper], "ok", network_ms=20), [paper]
    async def speed(_target, _url):
        return {"status": "ok", "elapsed_ms": 20000, "bytes_read": 1024,
                "kb_per_second": 1.0, "pdf_magic_valid": True, "error_code": None}
    monkeypatch.setattr(diagnostics, "_official_search", search)
    monkeypatch.setattr(diagnostics, "_speed_test_url", speed)
    row = asyncio.run(diagnostics.diagnose_platform("arxiv"))
    assert row["fulltext"]["status"] == "slow"
    assert "fulltext" not in row["auto_disabled_capabilities"]


def test_rxiv_connectivity_uses_official_metadata_api_even_when_local_index_empty(monkeypatch):
    class Resp:
        status_code = 200
        def json(self):
            return {"collection": [{
                "doi": "10.1101/396846", "version": "1",
                "title": "Official documented sample",
                "abstract": "A non-empty abstract from the official API.",
                "date": "2018-08-21",
            }]}
    class Client:
        async def get(self, url):
            assert url == "https://api.biorxiv.org/details/biorxiv/2018-08-21/2018-08-28/45/json"
            return Resp()
    class Limiter:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def fetch(self, client, method, url): return await client.get(url)
    monkeypatch.setattr(diagnostics, "get_search_http_client", lambda: Client())
    monkeypatch.setattr(diagnostics, "_RXIV_DIAGNOSTIC_LIMITER", Limiter())
    monkeypatch.setattr(diagnostics, "sync_status", lambda _source=None: {"indexed_count": 0})
    async def valid_pdf(_target, _url):
        return {
            "status": "ok", "elapsed_ms": 100, "bytes_read": 8192,
            "kb_per_second": 80.0, "pdf_magic_valid": True,
            "error_code": None,
        }
    monkeypatch.setattr(diagnostics, "_speed_test_url", valid_pdf)
    row = asyncio.run(diagnostics.diagnose_platform("biorxiv"))
    assert row["connectivity"]["status"] == "ok"
    assert row["search"]["status"] == "empty"
    assert row["auto_disabled_capabilities"] == []


def test_catalog_contains_all_sources_and_auxiliaries_with_explicit_capabilities(monkeypatch):
    monkeypatch.setattr(diagnostics, "get_settings", lambda: SimpleNamespace(search=SimpleNamespace(
        sources={}, paper_platform_contact_email="ops@example.org", crossref_email="", openalex_email="",
        openalex_api_key="", s2_api_key="", s2_license_confirmed=False,
        core_api_key="", core_license_confirmed=False)))
    monkeypatch.setattr(diagnostics, "sync_status", lambda *_args: {})
    rows = {row["id"]: row for row in diagnostics.source_catalog()}
    assert set(diagnostics.SOURCE_IDS) | {"unpaywall", "doi"} <= set(rows)
    assert rows["doi"]["supports_search"] is False
    assert rows["dblp"]["supports_abstract"] is False
    assert rows["openaire"]["official_docs_url"].startswith("https://graph.openaire.eu/")
    assert rows["biorxiv"]["diagnostic_method"] == "official_api_plus_controlled_pdf_probe"


def test_unknown_diagnostic_target_is_rejected():
    with pytest.raises(ValueError, match="未知检测目标"):
        asyncio.run(diagnostics.run_platform_diagnostics(["http://127.0.0.1/"]))


def test_speed_test_caps_ignored_range_and_checks_pdf(monkeypatch):
    monkeypatch.setattr(diagnostics, "_is_safe_url", lambda _url: (True, ""))
    class Response:
        status_code = 200; headers = {}
        async def aiter_bytes(self): yield b"%PDF-1.7" + b"x" * (diagnostics.DOWNLOAD_MAX_BYTES * 2)
    class Stream:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *_args): return False
    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        def stream(self, *args, **kwargs): return Stream()
    monkeypatch.setattr(diagnostics.httpx, "AsyncClient", Client)
    result = asyncio.run(diagnostics._speed_test_url("unpaywall", "https://oa.example/p.pdf"))
    assert result["status"] == "ok"
    assert result["bytes_read"] == diagnostics.DOWNLOAD_MAX_BYTES


def test_openaire_configured_credentials_auth_failure_is_distinct(monkeypatch):
    settings = SimpleNamespace(search=SimpleNamespace(
        openaire_client_id="client", openaire_client_secret="secret"))
    monkeypatch.setattr(diagnostics, "get_settings", lambda: settings)
    import tools.search.openaire as openaire
    async def no_token(): return ""
    monkeypatch.setattr(openaire, "_access_token", no_token)
    row = asyncio.run(diagnostics.diagnose_platform("openaire"))
    assert row["connectivity"]["error_code"] == "authentication_failed"
    assert set(row["auto_disabled_capabilities"]) == {"search", "abstract", "fulltext"}


def test_openaire_nested_description_parser_and_missing_pdf_only_affects_fulltext(monkeypatch):
    _ready(monkeypatch)
    from tools.search.openaire import parse_results
    papers = parse_results({"results": [{
        "id": "openaire-id", "mainTitle": {"value": "Paper title"},
        "descriptions": [{"value": "Nested abstract"}],
        "pids": [{"scheme": "doi", "value": "10.1/example"}],
    }]})
    async def search(_source):
        return SearchOutcome("openaire", papers, "ok", http_status=200, network_ms=10), papers
    monkeypatch.setattr(diagnostics, "_official_search", search)
    row = asyncio.run(diagnostics.diagnose_platform("openaire"))
    assert row["search"]["status"] == "ok"
    assert row["abstract"]["status"] == "ok"
    assert row["fulltext"]["error_code"] == "no_oa_candidate"
    assert row["auto_disabled_capabilities"] == ["fulltext"]


def test_run_platform_diagnostics_accepts_single_capability(monkeypatch):
    _ready(monkeypatch)
    paper = Paper(id="P", title="Paper", source="crossref", abstract="abstract")

    async def search(_source):
        return SearchOutcome(
            "crossref", [paper], "ok", http_status=200, network_ms=10,
        ), [paper]

    monkeypatch.setattr(diagnostics, "_official_search", search)
    rows = asyncio.run(diagnostics.run_platform_diagnostics(
        ["crossref"], "search",
    ))
    assert len(rows) == 1
    assert rows[0]["search"]["status"] == "ok"
    assert rows[0]["abstract"]["status"] == "not_applicable"
    assert rows[0]["_persist_capabilities"] == ["search"]


def test_unpaywall_connectivity_failure_closes_its_only_supported_capability(monkeypatch):
    _ready(monkeypatch)

    async def failed_candidate():
        return None, diagnostics._result(
            "failed", latency_ms=25, error_code="connection_error",
            message="official endpoint unavailable",
        )

    monkeypatch.setattr(diagnostics, "_unpaywall_candidate", failed_candidate)
    row = asyncio.run(diagnostics.diagnose_platform(
        "unpaywall", {"connectivity"},
    ))
    assert row["auto_disabled_capabilities"] == ["fulltext"]
    assert row["_persist_capabilities"] == ["fulltext"]
    items = diagnostics.flatten_capability_diagnostics([row])
    assert items[0]["capability"] == "fulltext"
    assert items[0]["status"] == "failed"
    assert items[0]["error_code"] == "connection_error"
    assert items[0]["auto_disable"] is True


def test_connectivity_closure_projects_failure_into_every_persisted_capability(monkeypatch):
    _ready(monkeypatch)

    async def search(_source):
        return SearchOutcome(
            "openaire", [], "connection_error", error_code="connection_error",
            network_ms=99,
        ), []

    monkeypatch.setattr(diagnostics, "_official_search", search)
    row = asyncio.run(diagnostics.diagnose_platform("openaire", {"search"}))
    items = diagnostics.flatten_capability_diagnostics([row])
    assert {item["capability"] for item in items} == {
        "search", "abstract", "fulltext",
    }
    assert all(item["status"] == "failed" for item in items)
    assert all(item["error_code"] == "connection_error" for item in items)


def test_unpaywall_no_candidate_keeps_connectivity_ok_but_closes_fulltext(monkeypatch):
    _ready(monkeypatch)

    async def no_candidate():
        return None, diagnostics._result(
            "ok", latency_ms=20, http_status=200, result_count=0,
            request_count=1, final_domain="api.unpaywall.org",
        )

    monkeypatch.setattr(diagnostics, "_unpaywall_candidate", no_candidate)
    row = asyncio.run(diagnostics.diagnose_platform("unpaywall"))
    assert row["connectivity"]["status"] == "ok"
    assert row["fulltext"]["status"] == "failed"
    assert row["fulltext"]["error_code"] == "no_oa_candidate"
    assert row["auto_disabled_capabilities"] == ["fulltext"]


def test_doi_connectivity_records_safe_redirect_destination(monkeypatch):
    class Response:
        status_code = 302
        headers = {"location": "https://journals.example/article"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, headers=None):
            assert url.startswith("https://doi.org/")
            assert headers == {"Accept": "text/html"}
            return Response()

    monkeypatch.setattr(diagnostics.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(diagnostics, "_is_safe_url", lambda _url: (True, ""))
    row = asyncio.run(diagnostics._doi_connectivity())
    assert row["status"] == "ok"
    assert row["request_count"] == 1
    assert row["redirect_count"] == 1
    assert row["final_domain"] == "journals.example"


def test_abstract_probe_search_prerequisite_failure_closes_search_only(monkeypatch):
    _ready(monkeypatch)

    async def search(_source):
        return SearchOutcome(
            "openaire", [], "reachable_empty", http_status=200,
            network_ms=20,
        ), []

    monkeypatch.setattr(diagnostics, "_official_search", search)
    row = asyncio.run(diagnostics.diagnose_platform(
        "openaire", {"abstract"},
    ))
    assert row["connectivity"]["status"] == "ok"
    assert row["search"]["error_code"] == "search_empty"
    assert row["abstract"]["error_code"] == "search_prerequisite_failed"
    assert row["auto_disabled_capabilities"] == ["search"]
    assert set(row["_persist_capabilities"]) == {"search", "abstract"}
    items = {
        item["capability"]: item
        for item in diagnostics.flatten_capability_diagnostics([row])
    }
    assert items["search"]["auto_disable"] is True
    assert items["abstract"]["auto_disable"] is False
