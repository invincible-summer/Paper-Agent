"""Offline checks pinned to providers' official documented API contracts.

These tests deliberately do not hit the network.  They ensure administrator
probes route through the same endpoints and request shapes described by each
provider's official documentation; a custom probe is allowed only for a
capability (Rxiv PDF representation) that the metadata API does not expose.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.models import Paper
from tools.search import diagnostics


def test_official_contract_registry_covers_every_matrix_target():
    assert set(diagnostics.OFFICIAL_PROVIDER_CONTRACTS) == set(diagnostics.SOURCE_IDS)
    required = {
        "docs", "endpoint", "method", "parameters", "response_list",
        "authentication", "rate_policy",
    }
    for contract in diagnostics.OFFICIAL_PROVIDER_CONTRACTS.values():
        assert required <= set(contract)
        assert contract["docs"].startswith("https://")
        assert contract["endpoint"].startswith("https://")
        assert contract["method"] in {"GET", "POST"}
        assert contract["parameters"]
        assert contract["response_list"]
        assert contract["authentication"]
        assert contract["rate_policy"]



def test_official_contracts_pin_documented_auth_parameters_and_rate_rules():
    contracts = diagnostics.OFFICIAL_PROVIDER_CONTRACTS
    assert {"search", "sort", "per_page", "api_key"} <= set(contracts["openalex"]["parameters"])
    assert contracts["semantic_scholar"]["authentication"] == "x-api-key_header"
    assert contracts["arxiv"]["rate_policy"] == "minimum_three_seconds_between_requests"
    assert {"tool", "email", "api_key"} <= set(contracts["pubmed"]["parameters"])
    assert contracts["openaire"]["authentication"] == "anonymous_or_oauth2_client_credentials"
    assert contracts["openaire"]["rate_policy"] == "anonymous_60_per_hour_authenticated_7200_per_hour"
    assert contracts["core"]["authentication"] == "bearer_header"
    assert "custom_pdf_fallback" not in contracts["biorxiv"]
    assert "custom_pdf_fallback" not in contracts["medrxiv"]


def test_fixed_queries_use_documented_provider_syntax_for_oa_samples():
    assert diagnostics.DIAGNOSTIC_QUERIES["openaire"] == "Introduction To Open Science"
    assert diagnostics.DIAGNOSTIC_QUERIES["arxiv"] == "electron"
    assert diagnostics.DIAGNOSTIC_QUERIES["doaj"] == 'bibjson.link.content_type:"application/pdf"'
    assert diagnostics.DIAGNOSTIC_QUERIES["datacite"] == "10.24416/uu01-4pe9nb"

def test_backend_endpoints_match_official_contract_registry():
    from tools.search import (
        arxiv, core, crossref, datacite, dblp, doaj, europepmc, hal,
        openaire, openalex, pubmed, semantic_scholar,
    )

    expected = diagnostics.OFFICIAL_PROVIDER_CONTRACTS
    assert openalex.API_URL == expected["openalex"]["endpoint"]
    assert semantic_scholar.API_URL == expected["semantic_scholar"]["endpoint"]
    assert arxiv.API_URL == expected["arxiv"]["endpoint"]
    assert crossref.API_URL == expected["crossref"]["endpoint"]
    assert europepmc.API_URL == expected["europepmc"]["endpoint"]
    assert doaj.API_URL == expected["doaj"]["endpoint"]
    assert hal.API_URL == expected["hal"]["endpoint"]
    assert openaire.API_URL == expected["openaire"]["endpoint"]
    assert core.API_URL == expected["core"]["endpoint"]
    assert pubmed.ESEARCH_URL == expected["pubmed"]["endpoint"]
    assert (pubmed.ESUMMARY_URL, pubmed.EFETCH_URL) == expected["pubmed"]["secondary_endpoints"]
    assert datacite.API_URL == expected["datacite"]["endpoint"]
    assert dblp.API_URLS[0] == expected["dblp"]["endpoint"]


def test_diagnostic_search_uses_provider_specific_documented_query(monkeypatch):
    seen = []

    class Backend:
        async def search_many(self, queries, limit):
            seen.append((queries, limit))
            return SimpleNamespace(
                source="openaire", papers=[Paper(
                    id="P", title="Open science", source="openaire",
                    abstract="documented response abstract",
                )], status="ok", http_status=200, request_count=1,
                network_ms=1, redirect_count=0, error_code=None,
                final_domain="api.openaire.eu",
            )

    monkeypatch.setitem(diagnostics.BACKENDS, "openaire", Backend())
    monkeypatch.setattr(
        diagnostics, "get_settings",
        lambda: SimpleNamespace(search=SimpleNamespace(
            openaire_client_id="", openaire_client_secret="",
        )),
    )
    outcome, papers = asyncio.run(diagnostics._official_search("openaire"))
    assert seen == [(["Introduction To Open Science"], 5)]
    assert outcome.status == "ok" and papers[0].title == "Open science"



def test_openaire_requires_official_header_and_results_schema():
    from tools.search.openaire import _valid_response_schema, parse_results

    assert _valid_response_schema({"header": {}, "results": []}) is True
    assert _valid_response_schema({"results": []}) is False
    assert _valid_response_schema({"header": {}, "results": {}}) is False

    papers = parse_results({
        "header": {"numFound": 1},
        "results": [{
            "id": "openaire::sample",
            "mainTitle": {"value": "Official Graph sample"},
            "descriptions": ["A string-list abstract from Graph API V3."],
            "pids": [{"scheme": "doi", "value": "10.1/example"}],
        }],
    })
    assert len(papers) == 1
    assert papers[0].title == "Official Graph sample"
    assert papers[0].abstract == "A string-list abstract from Graph API V3."
    assert papers[0].doi == "10.1/example"

def test_rxiv_documented_details_schema_is_parsed_not_just_http_200(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"collection": [{
                "doi": "10.1101/396846", "version": "1",
                "title": "Official sample", "abstract": "non-empty abstract",
                "authors": "A. Author; B. Author", "date": "2018-08-21",
            }]}

    class Client:
        async def get(self, url):
            assert url == (
                "https://api.biorxiv.org/details/biorxiv/"
                "2018-08-21/2018-08-28/45/json"
            )
            return Response()

    class Limiter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetch(self, client, method, url):
            assert method == "GET"
            return await client.get(url)

    monkeypatch.setattr(diagnostics, "get_search_http_client", lambda: Client())
    monkeypatch.setattr(diagnostics, "_RXIV_DIAGNOSTIC_LIMITER", Limiter())
    outcome = asyncio.run(diagnostics._rxiv_official_search("biorxiv"))
    assert outcome.status == "ok"
    assert outcome.papers[0].abstract == "non-empty abstract"
    assert outcome.papers[0].pdf_url is None
    assert outcome.papers[0].urls["biorxiv"] == (
        "https://www.biorxiv.org/content/10.1101/396846v1"
    )


def test_http_200_with_wrong_rxiv_schema_fails_schema_validation(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"unexpected": []}

    class Limiter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetch(self, *_args):
            return Response()

    monkeypatch.setattr(diagnostics, "_RXIV_DIAGNOSTIC_LIMITER", Limiter())
    monkeypatch.setattr(diagnostics, "get_search_http_client", lambda: object())
    outcome = asyncio.run(diagnostics._rxiv_official_search("biorxiv"))
    assert outcome.status == "schema_mismatch"
    assert outcome.papers == []


class _OfficialResponse:
    def __init__(self, payload=None, *, text="", status_code=200, host="api.example.org"):
        self._payload = payload
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.history = []
        self.extensions = {}
        self.url = SimpleNamespace(host=host)

    def json(self):
        return self._payload


class _OfficialClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class _ImmediateLimiter:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetch(self, client, method, url, **kwargs):
        return await client.request(method, url, **kwargs)


def _settings(**overrides):
    values = {
        "openalex_api_key": "openalex-key",
        "s2_api_key": "s2-key",
        "s2_license_confirmed": True,
        "core_api_key": "core-key",
        "core_license_confirmed": True,
        "paper_platform_contact_email": "ops@example.org",
        "crossref_email": "",
        "openalex_email": "",
        "ncbi_api_key": "ncbi-key",
        "openaire_client_id": "client-id",
        "openaire_client_secret": "client-secret",
    }
    values.update(overrides)
    return SimpleNamespace(search=SimpleNamespace(**values))


def _assert_documented_call(source, call, *, expected_params):
    method, url, kwargs = call
    contract = diagnostics.OFFICIAL_PROVIDER_CONTRACTS[source]
    assert method == contract["method"]
    assert url.startswith(contract["endpoint"])
    params = kwargs.get("params") or {}
    assert set(expected_params) <= set(params)
    assert set(params) <= set(contract["parameters"])


def test_official_search_backends_emit_documented_request_shapes(monkeypatch):
    from tools.search import (
        arxiv, core, crossref, datacite, dblp, doaj, europepmc, hal,
        openaire, openalex, semantic_scholar,
    )

    cases = [
        (
            "openalex", openalex, openalex.OpenAlexBackend,
            _OfficialResponse({"results": []}, host="api.openalex.org"),
            {"search", "sort", "per_page", "api_key"},
        ),
        (
            "semantic_scholar", semantic_scholar, semantic_scholar.SemanticScholarBackend,
            _OfficialResponse({"data": []}, host="api.semanticscholar.org"),
            {"query", "limit", "fields"},
        ),
        (
            "arxiv", arxiv, arxiv.ArxivBackend,
            _OfficialResponse(text=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
            ), host="export.arxiv.org"),
            {"search_query", "start", "max_results", "sortBy"},
        ),
        (
            "crossref", crossref, crossref.CrossrefBackend,
            _OfficialResponse({"message": {"items": []}}, host="api.crossref.org"),
            {"query", "rows", "select", "mailto"},
        ),
        (
            "europepmc", europepmc, europepmc.EuropePmcBackend,
            _OfficialResponse({"resultList": {"result": []}}, host="www.ebi.ac.uk"),
            {"query", "format", "pageSize", "resultType", "sort"},
        ),
        (
            "doaj", doaj, doaj.DoajBackend,
            _OfficialResponse({"results": []}, host="doaj.org"),
            {"page", "pageSize"},
        ),
        (
            "hal", hal, hal.HalBackend,
            _OfficialResponse({"response": {"docs": []}}, host="api.archives-ouvertes.fr"),
            {"q", "fl", "rows", "wt"},
        ),
        (
            "openaire", openaire, openaire.OpenAireBackend,
            _OfficialResponse({"header": {}, "results": []}, host="api.openaire.eu"),
            {"search", "type", "page", "pageSize"},
        ),
        (
            "core", core, core.CoreBackend,
            _OfficialResponse({"results": []}, host="api.core.ac.uk"),
            {"q", "limit"},
        ),
        (
            "datacite", datacite, datacite.DataCiteBackend,
            _OfficialResponse({"data": []}, host="api.datacite.org"),
            {"query", "resource-type-id", "page[size]"},
        ),
        (
            "dblp", dblp, dblp.DblpBackend,
            _OfficialResponse({"result": {"hits": {"hit": []}}}, host="dblp.org"),
            {"q", "format", "h"},
        ),
    ]

    for source, module, backend_cls, response, expected_params in cases:
        client = _OfficialClient([response])
        monkeypatch.setattr(module, "get_search_http_client", lambda client=client: client)
        if hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: _settings())
        if hasattr(module, "_limiter"):
            monkeypatch.setattr(module, "_limiter", _ImmediateLimiter())
        if source == "openaire":
            async def token():
                return "access-token"
            monkeypatch.setattr(module, "_access_token", token)
        asyncio.run(backend_cls().search_many(["machine learning"], 5))
        assert len(client.calls) == 1
        _assert_documented_call(source, client.calls[0], expected_params=expected_params)
        headers = client.calls[0][2].get("headers") or {}
        if source == "semantic_scholar":
            assert headers == {"x-api-key": "s2-key"}
        elif source == "core":
            assert headers == {"Authorization": "Bearer core-key"}
        elif source == "openaire":
            assert headers == {"Authorization": "Bearer access-token"}
        elif source == "dblp":
            assert "PaperAgent" in headers.get("User-Agent", "")
        if source == "doaj":
            assert client.calls[0][1].startswith(
                diagnostics.OFFICIAL_PROVIDER_CONTRACTS["doaj"]["endpoint"] + "/"
            )


def test_pubmed_uses_documented_esearch_then_efetch(monkeypatch):
    from tools.search import pubmed

    client = _OfficialClient([
        _OfficialResponse(
            {"esearchresult": {"idlist": ["123"]}},
            host="eutils.ncbi.nlm.nih.gov",
        ),
        _OfficialResponse(
            text=(
                '<?xml version="1.0"?>'
                '<PubmedArticleSet><PubmedArticle><MedlineCitation>'
                '<PMID>123</PMID><Article><ArticleTitle>Sample</ArticleTitle>'
                '</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>'
            ),
            host="eutils.ncbi.nlm.nih.gov",
        ),
    ])
    monkeypatch.setattr(pubmed, "get_search_http_client", lambda: client)
    monkeypatch.setattr(pubmed, "get_settings", lambda: _settings())
    limiter = _ImmediateLimiter()
    monkeypatch.setattr(pubmed, "_LIMITER_KEY", limiter)
    monkeypatch.setattr(pubmed, "_LIMITER_NO_KEY", limiter)

    outcome = asyncio.run(pubmed.PubMedBackend().search_many(["cancer"], 5))
    assert outcome.status == "ok"
    assert len(client.calls) == 2
    first_params = client.calls[0][2]["params"]
    second_params = client.calls[1][2]["params"]
    assert {"db", "tool", "email", "api_key", "term", "retmode", "retmax"} <= set(first_params)
    assert first_params["retmode"] == "json"
    assert {"db", "tool", "email", "api_key", "id", "retmode"} <= set(second_params)
    assert second_params["retmode"] == "xml"
    assert client.calls[0][1] == pubmed.ESEARCH_URL
    assert client.calls[1][1] == pubmed.EFETCH_URL


def test_arxiv_backend_enforces_official_three_second_spacing():
    from tools.search import arxiv

    assert arxiv.ARXIV_MAX_CONCURRENT == 1
    assert arxiv.ARXIV_MIN_INTERVAL_SECONDS >= 3.0
    assert arxiv._limiter._min_interval == arxiv.ARXIV_MIN_INTERVAL_SECONDS
