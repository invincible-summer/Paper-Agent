"""Administrator paper-platform capability diagnostics.

Every search/abstract check goes through the repository's official-API backend,
which follows the corresponding provider documentation.  PDF checks use a
provider-advertised OA URL; platforms that do not document/host this capability
are reported as ``not_applicable`` instead of being probed heuristically.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote, urljoin, urlparse

import httpx

from core.config import get_settings
from tools.pdf.fetcher import _is_safe_url
from core.models import Paper
from tools.search.base import (
    RateLimiter, SearchOutcome, classify_search_status, generate_paper_id,
)
from tools.search.http_client import get_search_http_client
from tools.search.manager import BACKENDS
from tools.search.registry import SOURCE_IDS, SOURCE_SPECS, contact_email, runtime_gate
from tools.search.rxiv_catalog import diagnostic_sample, sync_status

DIAGNOSTIC_QUERY = "retrieval augmented generation survey"
DIAGNOSTIC_QUERIES = {
    # Fixed public queries selected from each provider's documented search
    # syntax.  Full-text-capable sources use queries that return at least one
    # provider-declared OA PDF candidate in the official response.
    "openalex": "Attention Is All You Need",
    "semantic_scholar": "Attention Is All You Need",
    "arxiv": "electron",
    "crossref": "10.30554/archmed.21.1.4000.2021",
    "europepmc": "malaria",
    # DOAJ documents fielded path-search syntax; this fixed query requests
    # records whose official metadata explicitly advertises a PDF link.
    "doaj": 'bibjson.link.content_type:"application/pdf"',
    "hal": "machine learning",
    # This exact query appears in OpenAIRE's official Graph API V3 quickstart.
    "openaire": "Introduction To Open Science",
    "core": "machine learning",
    "pubmed": "cancer",
    "datacite": "10.24416/uu01-4pe9nb",
    "dblp": "database systems",
}

# Auditable documentation contracts used by the adapters.  Tests remain fully
# offline, but validate implementation endpoints/parameters against these
# official provider contracts rather than inventing test-only probe APIs.
OFFICIAL_PROVIDER_CONTRACTS = {
    "openalex": {
        "docs": "https://help.openalex.org/api/",
        "endpoint": "https://api.openalex.org/works", "method": "GET",
        "parameters": ("search", "sort", "per_page", "api_key"),
        "response_list": "results", "authentication": "api_key_query",
        "rate_policy": "official_api_key_credit_and_rate_limits",
    },
    "semantic_scholar": {
        "docs": "https://api.semanticscholar.org/api-docs/graph",
        "endpoint": "https://api.semanticscholar.org/graph/v1/paper/search", "method": "GET",
        "parameters": ("query", "limit", "fields"), "response_list": "data",
        "authentication": "x-api-key_header", "rate_policy": "provider_assigned",
    },
    "arxiv": {
        "docs": "https://info.arxiv.org/help/api/user-manual.html",
        "endpoint": "https://export.arxiv.org/api/query", "method": "GET",
        "parameters": ("search_query", "start", "max_results", "sortBy"),
        "response_list": "feed.entry", "authentication": "none",
        "rate_policy": "minimum_three_seconds_between_requests",
    },
    "crossref": {
        "docs": "https://www.crossref.org/documentation/retrieve-metadata/rest-api/",
        "endpoint": "https://api.crossref.org/works", "method": "GET",
        "parameters": ("query", "rows", "select", "mailto"),
        "response_list": "message.items", "authentication": "polite_mailto_optional",
        "rate_policy": "respect_response_rate_headers",
    },
    "europepmc": {
        "docs": "https://europepmc.org/RestfulWebService",
        "endpoint": "https://www.ebi.ac.uk/europepmc/webservices/rest/search", "method": "GET",
        "parameters": ("query", "format", "pageSize", "resultType", "sort"),
        "response_list": "resultList.result", "authentication": "none",
        "rate_policy": "provider_fair_use",
    },
    "doaj": {
        "docs": "https://doaj.org/api/docs",
        "endpoint": "https://doaj.org/api/search/articles", "method": "GET",
        "parameters": ("search_query_path", "page", "pageSize"),
        "response_list": "results", "authentication": "none",
        "rate_policy": "provider_fair_use",
    },
    "hal": {
        "docs": "https://api.archives-ouvertes.fr/docs/search",
        "endpoint": "https://api.archives-ouvertes.fr/search/", "method": "GET",
        "parameters": ("q", "fl", "rows", "wt"),
        "response_list": "response.docs", "authentication": "none",
        "rate_policy": "provider_fair_use",
    },
    "openaire": {
        "docs": "https://graph.openaire.eu/docs/apis/graph-api/quickstart/",
        "endpoint": "https://api.openaire.eu/graph/v3/research-products", "method": "GET",
        "parameters": ("search", "type", "page", "pageSize"),
        "response_list": "results", "authentication": "anonymous_or_oauth2_client_credentials",
        "rate_policy": "anonymous_60_per_hour_authenticated_7200_per_hour",
    },
    "core": {
        "docs": "https://api.core.ac.uk/docs/v3",
        "endpoint": "https://api.core.ac.uk/v3/search/works/", "method": "GET",
        "parameters": ("q", "limit"), "response_list": "results",
        "authentication": "bearer_header", "rate_policy": "provider_key_tier",
    },
    "biorxiv": {
        "docs": "https://api.biorxiv.org/",
        "endpoint": "https://api.biorxiv.org/details/biorxiv/", "method": "GET",
        "parameters": ("server_path", "start_date_path", "end_date_path", "cursor_path", "format_path"),
        "response_list": "collection", "authentication": "none",
        "rate_policy": "provider_fair_use", "custom_pdf_fallback": True,
    },
    "medrxiv": {
        "docs": "https://api.biorxiv.org/",
        "endpoint": "https://api.biorxiv.org/details/medrxiv/", "method": "GET",
        "parameters": ("server_path", "start_date_path", "end_date_path", "cursor_path", "format_path"),
        "response_list": "collection", "authentication": "none",
        "rate_policy": "provider_fair_use", "custom_pdf_fallback": True,
    },
    "pubmed": {
        "docs": "https://www.ncbi.nlm.nih.gov/books/NBK25501/",
        "endpoint": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", "method": "GET",
        "secondary_endpoints": (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        ),
        "parameters": ("db", "tool", "email", "term", "retmode", "retmax", "api_key"),
        "response_list": "esearchresult.idlist", "authentication": "contact_email_and_optional_api_key",
        "rate_policy": "three_per_second_or_ten_with_api_key",
    },
    "datacite": {
        "docs": "https://support.datacite.org/docs/api",
        "endpoint": "https://api.datacite.org/dois", "method": "GET",
        "parameters": ("query", "resource-type-id", "page[size]"),
        "response_list": "data", "authentication": "none_for_public_metadata",
        "rate_policy": "provider_fair_use",
    },
    "dblp": {
        "docs": "https://dblp.org/faq/How+to+use+the+dblp+search+API.html",
        "endpoint": "https://dblp.org/search/publ/api", "method": "GET",
        "parameters": ("q", "format", "h"), "response_list": "result.hits.hit",
        "authentication": "descriptive_user_agent", "rate_policy": "provider_fair_use",
    },
    "unpaywall": {
        "docs": "https://unpaywall.org/products/api",
        "endpoint": "https://api.unpaywall.org/v2/", "method": "GET",
        "parameters": ("doi_path", "email"),
        "response_list": "best_oa_location_or_oa_locations",
        "authentication": "contact_email_query", "rate_policy": "provider_fair_use",
    },
    "doi": {
        "docs": "https://doi.org/help.html",
        "endpoint": "https://doi.org/", "method": "GET",
        "parameters": ("doi_path", "Accept"), "response_list": "redirect_location",
        "authentication": "none", "rate_policy": "provider_fair_use",
    },
}
DOWNLOAD_MAX_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 20.0
SLOW_LATENCY_MS = 8000
SLOW_DOWNLOAD_KBPS = 50.0
AUXILIARY_TARGETS = ("unpaywall", "doi")
_ALL_TARGETS = set(SOURCE_IDS) | set(AUXILIARY_TARGETS)
_RXIV_DIAGNOSTIC_LIMITER = RateLimiter(max_concurrent=1, min_interval=1.0, fast_fail_429=True)
_RXIV_DIAGNOSTIC_DATES = {
    # These are examples published by the official Rxiv API documentation.
    "biorxiv": ("2018-08-21", "2018-08-28", "45"),
    "medrxiv": ("2020-03-21", "2020-03-24", "45"),
}

# Central, auditable fixed samples.  Search endpoints/parameters remain defined
# by each official backend; these identifiers are only used for documented OA
# resolution/download validation.
OFFICIAL_DIAGNOSTIC_SAMPLES = {
    "arxiv": {"id": "1706.03762", "pdf_url": "https://arxiv.org/pdf/1706.03762.pdf"},
    "doi": {"doi": "10.1371/journal.pone.0000308", "url": "https://doi.org/10.1371/journal.pone.0000308"},
    "unpaywall": {"doi": "10.1371/journal.pone.0000308"},
    "biorxiv": {"doi": "10.1101/396846", "version": 1},
    "medrxiv": {"doi": "10.1101/2020.09.09.20191205", "version": 1},
}


def _result(status: str, *, latency_ms: int | None = None, error_code: str | None = None,
            message: str | None = None, **metrics) -> dict:
    row = {"status": status, "latency_ms": latency_ms, "error_code": error_code, "message": message}
    row.update(metrics)
    return row


def _configuration_status(source: str) -> tuple[bool, str]:
    if source == "openaire":
        settings = get_settings().search
        client_id = bool(getattr(settings, "openaire_client_id", ""))
        client_secret = bool(getattr(settings, "openaire_client_secret", ""))
        if client_id != client_secret:
            return False, "incomplete_credentials"
    if source in {"biorxiv", "medrxiv"}:
        return True, "ready"
    if source in SOURCE_IDS:
        return runtime_gate(source)
    if source == "unpaywall":
        return (True, "ready") if contact_email(get_settings().search) else (False, "missing_contact_email")
    return True, "ready"


def source_catalog() -> list[dict]:
    settings = get_settings().search
    try:
        rxiv = sync_status()
    except Exception:
        rxiv = {name: {"server": name, "indexed_count": 0, "min_published": None,
                       "max_published": None, "last_success_at": None,
                       "last_error": "index_unavailable"} for name in ("biorxiv", "medrxiv")}
    rows: list[dict] = []
    for source, spec in SOURCE_SPECS.items():
        ready, reason = runtime_gate(source, settings)
        local = rxiv.get(source) if source in {"biorxiv", "medrxiv"} else None
        configured, configuration_status = _configuration_status(source)
        if source == "openaire":
            client_id = bool(getattr(settings, "openaire_client_id", ""))
            client_secret = bool(getattr(settings, "openaire_client_secret", ""))
            key_configured = client_id and client_secret
        elif spec.requires_key:
            key_configured = bool(getattr(settings, spec.requires_key.lower(), ""))
        elif source == "pubmed":
            key_configured = bool(contact_email(settings))
        else:
            key_configured = True
        if spec.requires_license_confirmation:
            confirmed = bool(getattr(settings, spec.requires_license_confirmation.lower(), False))
            license_status = "confirmed" if confirmed else "not_confirmed"
        else:
            license_status = "not_required"
        rows.append({
            "id": source, "display_name": spec.display_name, "coverage": spec.coverage,
            "protocol": spec.protocol, "license_status": spec.license_status,
            "routing_tags": list(spec.routing_tags), "requires_key": bool(spec.requires_key),
            "key_configured": key_configured, "configuration_status": configuration_status,
            "operational_status": (
                "ready" if configured and ready else configuration_status if not configured else reason
            ),
            "operational_reason": (
                "ready" if configured and ready else configuration_status if not configured else reason
            ),
            "requires_license_confirmation": bool(spec.requires_license_confirmation),
            "license_confirmation_status": license_status,
            "supports_remote_search": spec.supports_remote_search, "supports_search": True,
            "supports_connectivity": True, "supports_abstract": spec.supports_abstract,
            "supports_pdf_probe": spec.supports_pdf_probe,
            "supports_fulltext": spec.supports_fulltext,
            "supports_download_test": spec.supports_download_test,
            "official_docs_url": OFFICIAL_PROVIDER_CONTRACTS[source]["docs"],
            "diagnostic_method": (
                "official_api_plus_controlled_pdf_probe"
                if source in {"biorxiv", "medrxiv"}
                else "official_api"
            ),
            "local_index_status": local,
        })
    email = contact_email(settings)
    rows.extend([
        {"id": "unpaywall", "display_name": "Unpaywall", "coverage": "DOI → OA 全文候选",
         "protocol": "REST API v2", "license_status": "开放 API", "routing_tags": [],
         "requires_key": False, "key_configured": bool(email),
         "configuration_status": "ready" if email else "missing_contact_email",
         "operational_status": "ready" if email else "missing_contact_email",
         "operational_reason": "ready" if email else "missing_contact_email",
         "requires_license_confirmation": False, "license_confirmation_status": "not_required",
         "supports_remote_search": False, "supports_search": False, "supports_connectivity": True,
         "supports_abstract": False, "supports_pdf_probe": True, "supports_fulltext": True,
         "supports_download_test": True,
         "official_docs_url": OFFICIAL_PROVIDER_CONTRACTS["unpaywall"]["docs"],
         "diagnostic_method": "official_api", "local_index_status": None},
        {"id": "doi", "display_name": "doi.org", "coverage": "DOI 官方解析与重定向",
         "protocol": "HTTPS resolver", "license_status": "公共解析服务", "routing_tags": [],
         "requires_key": False, "key_configured": True, "configuration_status": "ready",
         "operational_status": "ready", "operational_reason": "ready",
         "requires_license_confirmation": False, "license_confirmation_status": "not_required",
         "supports_remote_search": False, "supports_search": False, "supports_connectivity": True,
         "supports_abstract": False, "supports_pdf_probe": False, "supports_fulltext": False,
         "supports_download_test": False,
         "official_docs_url": OFFICIAL_PROVIDER_CONTRACTS["doi"]["docs"],
         "diagnostic_method": "official_resolver", "local_index_status": None},
    ])
    return rows


def _validate_sources(sources: list[str] | None) -> list[str]:
    selected = (
        list(tuple(SOURCE_IDS) + AUXILIARY_TARGETS)
        if sources is None
        else list(dict.fromkeys(sources))
    )
    unknown = [name for name in selected if name not in _ALL_TARGETS]
    if unknown:
        raise ValueError(f"未知检测目标：{', '.join(unknown)}")
    return selected


async def _rxiv_official_search(source: str) -> SearchOutcome:
    """Validate the documented Rxiv details endpoint and parse real records."""
    start_date, end_date, cursor = _RXIV_DIAGNOSTIC_DATES[source]
    url = f"https://api.biorxiv.org/details/{source}/{start_date}/{end_date}/{cursor}/json"
    started = time.monotonic()
    try:
        async with _RXIV_DIAGNOSTIC_LIMITER:
            resp = await _RXIV_DIAGNOSTIC_LIMITER.fetch(
                get_search_http_client(), "GET", url
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        data = resp.json() if resp.status_code == 200 else {}
        collection = data.get("collection") if isinstance(data, dict) else None
        if resp.status_code != 200:
            return SearchOutcome(
                source, [], "http_error", http_status=resp.status_code,
                request_count=1, network_ms=elapsed_ms,
                error_code=f"http_{resp.status_code}",
                final_domain=urlparse(url).hostname,
            )
        if not isinstance(collection, list):
            return SearchOutcome(
                source, [], "schema_mismatch", http_status=200, request_count=1,
                network_ms=elapsed_ms, error_code="schema_mismatch",
                final_domain=urlparse(url).hostname,
            )
        papers: list[Paper] = []
        for item in collection:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            doi = str(item.get("doi") or "").strip().lower()
            if not title or not doi:
                continue
            version_raw = item.get("version") or 1
            try:
                version = max(1, int(version_raw))
            except (TypeError, ValueError):
                version = 1
            authors_raw = str(item.get("authors") or item.get("author_corresponding") or "")
            authors = [part.strip() for part in authors_raw.replace(";", ",").split(",") if part.strip()]
            date = str(item.get("date") or "")
            year = int(date[:4]) if date[:4].isdigit() else None
            host = "www.biorxiv.org" if source == "biorxiv" else "www.medrxiv.org"
            landing = f"https://{host}/content/{doi}v{version}"
            # The details API does not expose a PDF field.  This is therefore
            # the explicitly documented exception to official-API-first: a
            # controlled provider-hosted ``.full.pdf`` representation, still
            # subject to the shared SSRF guard and bounded PDF validation.
            pdf_url = f"{landing}.full.pdf"
            papers.append(Paper(
                id=generate_paper_id(title, authors[0] if authors else "", year, doi),
                title=title, authors=authors, year=year, venue=source, doi=doi,
                source=source, abstract=str(item.get("abstract") or "").strip(),
                pdf_url=pdf_url, urls={source: landing},
            ))
        status = "ok" if papers else "reachable_empty"
        return SearchOutcome(
            source, papers, status, http_status=200, request_count=1,
            network_ms=elapsed_ms, final_domain=urlparse(url).hostname,
        )
    except httpx.TimeoutException:
        return SearchOutcome(
            source, [], "timeout", error_code="timeout",
            network_ms=int((time.monotonic() - started) * 1000),
        )
    except httpx.RequestError:
        return SearchOutcome(
            source, [], "connection_error", error_code="connection_error",
            network_ms=int((time.monotonic() - started) * 1000),
        )
    except (TypeError, ValueError):
        return SearchOutcome(
            source, [], "schema_mismatch", error_code="schema_mismatch",
            network_ms=int((time.monotonic() - started) * 1000),
        )


async def _official_search(source: str) -> tuple[SearchOutcome, list]:
    if source == "openaire":
        from tools.search import openaire as openaire_module
        settings = get_settings().search
        if getattr(settings, "openaire_client_id", "") and getattr(settings, "openaire_client_secret", ""):
            started = time.monotonic()
            try:
                token = await openaire_module._access_token()
            except Exception:
                token = ""
            if not token:
                return SearchOutcome("openaire", [], "authentication_failed",
                                     error_code="authentication_failed",
                                     network_ms=int((time.monotonic()-started)*1000)), []
    if source in {"biorxiv", "medrxiv"}:
        outcome = await _rxiv_official_search(source)
        return outcome, outcome.papers
    backend = BACKENDS[source]
    started = time.monotonic()
    try:
        if source == "pubmed" and hasattr(backend, "search_for_diagnostic"):
            backend._reset_telemetry()
            query = DIAGNOSTIC_QUERIES.get(source, DIAGNOSTIC_QUERY)
            papers = await asyncio.wait_for(
                backend.search_for_diagnostic(query, 5), 20
            )
            status, error = classify_search_status(
                papers, getattr(backend, "_last_http_status", None),
                forced_status=getattr(backend, "_forced_status", None),
                content_type=getattr(backend, "_last_content_type", ""),
            )
            outcome = SearchOutcome(
                source, papers, status,
                http_status=getattr(backend, "_last_http_status", None),
                request_count=int(
                    getattr(backend, "_last_request_count", 0) or 0
                ),
                network_ms=int((time.monotonic() - started) * 1000),
                error_code=error,
                redirect_count=int(
                    getattr(backend, "_last_redirect_count", 0) or 0
                ),
                final_domain=getattr(backend, "_last_final_domain", None),
            )
            return outcome, papers
        if hasattr(backend, "search_many"):
            query = DIAGNOSTIC_QUERIES.get(source, DIAGNOSTIC_QUERY)
            outcome = await asyncio.wait_for(backend.search_many([query], 5), 20)
            return outcome, outcome.papers
        query = DIAGNOSTIC_QUERIES.get(source, DIAGNOSTIC_QUERY)
        papers = await asyncio.wait_for(backend.search(query, 5), 20)
        status, error = classify_search_status(
            papers, getattr(backend, "_last_http_status", None),
            forced_status=getattr(backend, "_forced_status", None),
            content_type=getattr(backend, "_last_content_type", ""),
        )
        outcome = SearchOutcome(
            source, papers, status, http_status=getattr(backend, "_last_http_status", None),
            request_count=max(1, int(getattr(backend, "_last_request_count", 0) or 1)),
            network_ms=int((time.monotonic() - started) * 1000), error_code=error,
            redirect_count=int(getattr(backend, "_last_redirect_count", 0) or 0),
            final_domain=getattr(backend, "_last_final_domain", None),
        )
        return outcome, papers
    except asyncio.TimeoutError:
        return SearchOutcome(source, [], "timeout", error_code="timeout", network_ms=int((time.monotonic()-started)*1000)), []
    except Exception:
        return SearchOutcome(source, [], "connection_error", error_code="connection_error", network_ms=int((time.monotonic()-started)*1000)), []


def _connectivity_diagnostic(outcome: SearchOutcome) -> dict:
    if outcome.status in {"ok", "reachable_empty"}:
        status = "slow" if outcome.network_ms >= SLOW_LATENCY_MS else "ok"
        return _result(status, latency_ms=outcome.network_ms, http_status=outcome.http_status,
                       request_count=outcome.request_count, final_domain=outcome.final_domain,
                       redirect_count=outcome.redirect_count)
    return _result("failed", latency_ms=outcome.network_ms,
                   http_status=outcome.http_status, request_count=outcome.request_count,
                   final_domain=outcome.final_domain, redirect_count=outcome.redirect_count,
                   error_code=outcome.error_code or outcome.status,
                   message="官方接口基础连通或响应结构检测失败。")

def _search_diagnostic(outcome: SearchOutcome) -> dict:
    if outcome.status == "ok" and outcome.papers:
        status = "slow" if outcome.network_ms >= SLOW_LATENCY_MS else "ok"
        return _result(status, latency_ms=outcome.network_ms, result_count=len(outcome.papers),
                       request_count=outcome.request_count, http_status=outcome.http_status,
                       final_domain=outcome.final_domain, redirect_count=outcome.redirect_count)
    if outcome.status == "reachable_empty":
        return _result("failed", latency_ms=outcome.network_ms, error_code="search_empty",
                       message="官方搜索接口可达，但固定公开查询未返回合法论文结果。",
                       result_count=0, request_count=outcome.request_count, http_status=outcome.http_status)
    return _result("failed", latency_ms=outcome.network_ms,
                   error_code=outcome.error_code or outcome.status,
                   message="官方搜索接口检测失败。", result_count=0,
                   request_count=outcome.request_count, http_status=outcome.http_status)


def _abstract_diagnostic(source: str, papers: list, search: dict) -> dict:
    if not SOURCE_SPECS[source].supports_abstract:
        return _result("not_applicable", message="该平台官方搜索记录不提供可验证摘要。")
    if search["status"] == "failed":
        return _result("failed", latency_ms=search.get("latency_ms"), error_code="search_prerequisite_failed",
                       message="搜索前置检测失败，无法验证摘要。")
    abstracts = [(getattr(p, "id", ""), (getattr(p, "abstract", "") or "").strip()) for p in papers]
    valid = [(pid, text) for pid, text in abstracts if text]
    if not valid:
        return _result("failed", latency_ms=search.get("latency_ms"), error_code="abstract_empty",
                       message="固定样本未返回有效非空摘要。", valid_abstract_count=0)
    sample_id, text = max(valid, key=lambda item: len(item[1]))
    return _result("ok", latency_ms=search.get("latency_ms"), valid_abstract_count=len(valid),
                   abstract_length=len(text), sample_id=sample_id)


async def _doi_connectivity() -> dict:
    started = time.monotonic()
    url = OFFICIAL_DIAGNOSTIC_SAMPLES["doi"]["url"]
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, proxy=None, trust_env=False) as client:
            resp = await client.get(url, headers={"Accept": "text/html"})
        elapsed = int((time.monotonic() - started) * 1000)
        location = resp.headers.get("location") or ""
        destination = urljoin(url, location) if location else ""
        safe, _reason = _is_safe_url(destination) if destination else (False, "")
        ok = (
            resp.status_code in {301, 302, 303, 307, 308}
            and bool(destination)
            and safe
        )
        return _result(
            "ok" if ok else "failed", latency_ms=elapsed,
            http_status=resp.status_code, request_count=1,
            redirect_count=1 if location else 0,
            final_domain=(urlparse(destination).hostname or "") if ok else "doi.org",
            error_code=None if ok else "doi_resolution_failed",
        )
    except Exception as exc:  # noqa: BLE001
        return _result("failed", latency_ms=int((time.monotonic()-started)*1000),
                       error_code="connection_error", message=type(exc).__name__)


async def _unpaywall_candidate() -> tuple[str | None, dict]:
    email = contact_email(get_settings().search)
    if not email:
        return None, _result("not_configured", error_code="missing_contact_email",
                             message="Unpaywall 官方 API 要求提供联系邮箱。")
    doi = OFFICIAL_DIAGNOSTIC_SAMPLES["unpaywall"]["doi"]
    started = time.monotonic()
    try:
        url = f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}"
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, proxy=None, trust_env=False) as client:
            resp = await client.get(url, params={"email": email})
        elapsed = int((time.monotonic() - started) * 1000)
        data = resp.json() if resp.status_code == 200 else {}
        if resp.status_code != 200 or not isinstance(data, dict):
            return None, _result(
                "failed", latency_ms=elapsed, http_status=resp.status_code,
                request_count=1, redirect_count=0,
                final_domain="api.unpaywall.org",
                error_code=f"http_{resp.status_code}",
            )
        best = data.get("best_oa_location") or {}
        candidate = best.get("url_for_pdf")
        if not candidate:
            candidate = next((
                location.get("url_for_pdf")
                for location in data.get("oa_locations") or []
                if isinstance(location, dict) and location.get("url_for_pdf")
            ), None)
        return candidate, _result(
            "ok", latency_ms=elapsed, http_status=200, request_count=1,
            redirect_count=0, final_domain="api.unpaywall.org",
            result_count=1 if candidate else 0, error_code=None,
            message=None if candidate else (
                "Unpaywall 官方接口可达，但固定 OA 样本未返回 PDF 候选。"
            ),
        )
    except Exception:
        return None, _result("failed", latency_ms=int((time.monotonic()-started)*1000), error_code="connection_error")


async def diagnose_platform(source: str, capabilities: set[str] | None = None) -> dict:
    if source not in _ALL_TARGETS:
        raise ValueError(f"未知检测目标：{source}")
    requested = capabilities or {"connectivity", "search", "abstract", "fulltext"}
    persist_capabilities = (
        {"search", "abstract", "fulltext"}
        if capabilities is None
        else set(capabilities) & {"search", "abstract", "fulltext"}
    )
    row = {"source": source, "connectivity": _result("not_applicable"),
           "search": _result("not_applicable"), "abstract": _result("not_applicable"),
           "fulltext": _result("not_applicable"), "auto_disabled_capabilities": [],
           "_persist_capabilities": sorted(persist_capabilities)}
    configured, config_reason = _configuration_status(source)
    if not configured:
        for capability in requested:
            if capability in row:
                row[capability] = _result("not_configured", error_code=config_reason,
                                          message="平台静态配置未满足；不会自动修改管理员开关。")
        return row
    if source == "doi":
        row["connectivity"] = await _doi_connectivity()
        return row
    if source == "unpaywall":
        candidate, connectivity = await _unpaywall_candidate()
        row["connectivity"] = connectivity
        test_fulltext = capabilities is None or "fulltext" in requested
        if test_fulltext:
            if candidate:
                row["fulltext"] = await _fulltext_diagnostic(
                    source, candidate
                )
            elif connectivity["status"] == "failed":
                row["fulltext"] = dict(connectivity)
            else:
                row["fulltext"] = _result(
                    "failed", latency_ms=connectivity.get("latency_ms"),
                    error_code="no_oa_candidate",
                    message="官方 OA 解析成功，但未返回可验证 PDF 候选。",
                )
            row["_persist_capabilities"] = ["fulltext"]
        if connectivity["status"] == "failed":
            row["auto_disabled_capabilities"] = ["fulltext"]
            row["_persist_capabilities"] = ["fulltext"]
        elif test_fulltext and row["fulltext"]["status"] == "failed":
            row["auto_disabled_capabilities"] = ["fulltext"]
        return row

    outcome, papers = await _official_search(source)
    if outcome.status == "local_budget_exhausted":
        message = (
            "已达到本进程为官方匿名调用额度保留的安全上限；"
            "这不是平台网络故障，不会自动修改能力开关。"
        )
        for capability in requested:
            if capability in row:
                row[capability] = _result(
                    "not_configured", error_code="local_budget_exhausted",
                    message=message,
                )
        return row
    search = _search_diagnostic(outcome)
    # Search itself proves base connectivity through the documented endpoint.
    row["connectivity"] = _connectivity_diagnostic(outcome)
    test_search = capabilities is None or "search" in requested
    test_abstract = capabilities is None or "abstract" in requested
    test_fulltext = capabilities is None or "fulltext" in requested
    if source in {"biorxiv", "medrxiv"}:
        try:
            indexed = int((sync_status(source) or {}).get("indexed_count") or 0)
        except Exception:
            indexed = 0
        if indexed <= 0:
            local_search = _result(
                "empty", error_code="index_empty",
                message="本地官方元数据索引为空；不视为跨境网络失败。",
                result_count=0,
            )
        else:
            started_local = time.monotonic()
            try:
                sample = diagnostic_sample(source)
                query = " ".join((sample.title if sample else "").split()[:4])
                local_papers = await BACKENDS[source].search(query, 5) if query else []
                local_latency = int((time.monotonic() - started_local) * 1000)
                if local_papers and all(
                    getattr(paper, "title", "") and getattr(paper, "id", "")
                    for paper in local_papers
                ):
                    local_search = _result(
                        "ok", latency_ms=local_latency,
                        result_count=len(local_papers), request_count=0,
                    )
                else:
                    local_search = _result(
                        "failed", latency_ms=local_latency,
                        error_code="local_search_empty",
                        message="本地索引非空，但真实 FTS 查询未返回合法论文记录。",
                        result_count=0, request_count=0,
                    )
            except Exception:
                local_search = _result(
                    "failed", latency_ms=int((time.monotonic() - started_local) * 1000),
                    error_code="local_index_error",
                    message="本地论文索引查询失败。", result_count=0, request_count=0,
                )
        # Abstract validity is checked against a real record returned by the
        # documented details endpoint, independently of local FTS health.
        local_abstract = _abstract_diagnostic(source, papers, search)
        if test_search:
            row["search"] = local_search
        if test_abstract:
            row["abstract"] = local_abstract
    else:
        if test_search:
            row["search"] = search
        if test_abstract:
            row["abstract"] = _abstract_diagnostic(source, papers, search)
    if test_fulltext:
        if not SOURCE_SPECS[source].supports_fulltext:
            row["fulltext"] = _result(
                "not_applicable",
                message="该平台不托管或不声明可验证全文下载。",
            )
        else:
            candidate = _pdf_candidate(source, papers)
            if not candidate and search["status"] == "failed":
                row["fulltext"] = _result(
                    "failed", latency_ms=search.get("latency_ms"),
                    error_code="search_prerequisite_failed",
                    message="搜索前置检测失败，未执行独立 PDF 下载判定。",
                )
            else:
                row["fulltext"] = await _fulltext_diagnostic(source, candidate)

    # A single abstract/fulltext diagnostic necessarily executes the official
    # search prerequisite. If that real search itself fails (while base
    # connectivity remains healthy), persist and close search only; the
    # dependent capability was not actually tested and must not be closed.
    prerequisite_search_failed = (
        source not in {"biorxiv", "medrxiv"}
        and search["status"] == "failed"
        and row["connectivity"]["status"] != "failed"
        and not test_search
    )
    if prerequisite_search_failed:
        row["search"] = search
        persist_capabilities.add("search")

    # Fixed closure rules. Configuration/not-applicable/slow and dependent
    # probes that never ran never close a capability.
    if row["connectivity"]["status"] == "failed":
        row["auto_disabled_capabilities"] = [
            capability
            for capability in ("search", "abstract", "fulltext")
            if getattr(
                SOURCE_SPECS[source], f"supports_{capability}", True
            )
        ]
        row["_persist_capabilities"] = list(row["auto_disabled_capabilities"])
    else:
        for capability in persist_capabilities:
            metric = row[capability]
            if (metric["status"] == "failed"
                    and metric.get("error_code") != "search_prerequisite_failed"):
                row["auto_disabled_capabilities"].append(capability)
        row["_persist_capabilities"] = sorted(persist_capabilities)
    return row


_PREFERRED_PDF_HOSTS = {
    # Prefer stable repository copies returned by the provider over publisher
    # landing/download URLs that commonly reject bounded Range probes.
    "openalex": ("arxiv.org", "www.arxiv.org", "export.arxiv.org"),
    "semantic_scholar": ("arxiv.org", "www.arxiv.org", "export.arxiv.org"),
    "europepmc": ("europepmc.org", "www.ebi.ac.uk"),
    "hal": ("hal.science", "theses.hal.science", "inria.hal.science"),
    "core": ("core.ac.uk", "api.core.ac.uk"),
}


def _pdf_candidate(source: str, papers: list[Paper]) -> str | None:
    """Choose one deterministic provider-declared PDF from the fixed sample."""
    if source == "arxiv":
        return OFFICIAL_DIAGNOSTIC_SAMPLES["arxiv"]["pdf_url"]
    candidates = list(dict.fromkeys(
        str(paper.pdf_url)
        for paper in papers
        if getattr(paper, "pdf_url", None)
    ))
    preferred = _PREFERRED_PDF_HOSTS.get(source, ())
    if preferred:
        for host in preferred:
            for candidate in candidates:
                if (urlparse(candidate).hostname or "").lower() == host:
                    return candidate
    return candidates[0] if candidates else None


async def _fulltext_diagnostic(source: str, candidate: str | None) -> dict:
    if not candidate:
        return _result("failed", error_code="no_oa_candidate",
                       message="官方结果未提供可验证的 OA PDF 候选。")
    result = await _speed_test_url(source, candidate)
    status = result.get("status")
    if status == "ok" and float(result.get("kb_per_second") or 0) < SLOW_DOWNLOAD_KBPS:
        result["status"] = "slow"
        result["message"] = "PDF 有效，但下载速度偏慢；不会自动关闭全文能力。"
    result["latency_ms"] = result.get("elapsed_ms")
    return result


async def run_platform_diagnostics(sources: list[str] | None = None, capability: str | None = None) -> list[dict]:
    selected = _validate_sources(sources)
    if capability is not None and capability not in {"connectivity", "search", "abstract", "fulltext"}:
        raise ValueError(f"未知平台能力：{capability}")
    requested = {capability} if capability else None
    return await asyncio.gather(*(diagnose_platform(source, requested) for source in selected))


async def _speed_test_url(target: str, url: str) -> dict:
    from tools.pdf.arxiv_wget import fetch_arxiv_pdf
    if (urlparse(url).hostname or "").lower() in {
        "arxiv.org", "www.arxiv.org", "export.arxiv.org",
    }:
        result = await fetch_arxiv_pdf(url, max_bytes=DOWNLOAD_MAX_BYTES,
                                       timeout=DOWNLOAD_TIMEOUT_SECONDS, probe=True)
        return {"target": target, "status": result.status, "domain": urlparse(url).hostname or "",
                "http_status": None, "bytes_read": result.bytes_read,
                "elapsed_ms": result.elapsed_ms, "kb_per_second": result.kb_per_second,
                "pdf_magic_valid": result.pdf_magic_valid, "redirects": 0,
                "error_code": result.error_code, "exit_code": result.exit_code}
    started = time.monotonic(); current = url; redirects = 0; total = 0; magic = b""; http_status = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS), follow_redirects=False,
                                     proxy=None, trust_env=False) as client:
            while redirects <= 5:
                safe, _reason = _is_safe_url(current)
                if not safe: raise ValueError("unsafe_url")
                async with client.stream("GET", current, headers={"Range": f"bytes=0-{DOWNLOAD_MAX_BYTES - 1}"}) as resp:
                    http_status = int(resp.status_code)
                    if http_status in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("location")
                        if not location: raise ValueError("redirect_failed")
                        current = urljoin(current, location); redirects += 1; continue
                    if http_status not in {200, 206}: raise ValueError(f"http_{http_status}")
                    async for chunk in resp.aiter_bytes():
                        if not chunk: continue
                        if len(magic) < 5: magic += chunk[:5-len(magic)]
                        remaining = DOWNLOAD_MAX_BYTES - total
                        total += min(len(chunk), remaining)
                        if total >= DOWNLOAD_MAX_BYTES: break
                break
        elapsed = max(.001, time.monotonic()-started); valid = magic.startswith(b"%PDF-")
        return {"target": target, "status": "ok" if valid else "failed", "domain": urlparse(current).hostname or "",
                "http_status": http_status, "bytes_read": total, "elapsed_ms": int(elapsed*1000),
                "kb_per_second": round(total/1024/elapsed, 1), "pdf_magic_valid": valid,
                "redirects": redirects, "error_code": None if valid else "not_pdf"}
    except Exception as exc:  # noqa: BLE001
        elapsed=max(.001,time.monotonic()-started); code=str(exc) or type(exc).__name__
        return {"target":target,"status":"failed","domain":urlparse(current).hostname or "",
                "http_status":http_status,"bytes_read":total,"elapsed_ms":int(elapsed*1000),
                "kb_per_second":round(total/1024/elapsed,1),"pdf_magic_valid":False,
                "redirects":redirects,"error_code":code[:80]}


def flatten_capability_diagnostics(platforms: list[dict]) -> list[dict]:
    items: list[dict] = []
    for platform in platforms:
        source = platform["source"]
        connectivity_failed = platform["connectivity"].get("status") == "failed"
        auto = set(platform.get("auto_disabled_capabilities") or [])
        for capability in platform.get(
            "_persist_capabilities", ("search", "abstract", "fulltext")
        ):
            if connectivity_failed and capability in auto:
                metric = dict(platform["connectivity"])
                metric["status"] = "failed"
            else:
                metric = dict(platform[capability])
            metric.update({
                "source": source,
                "capability": capability,
                "auto_disable": capability in auto,
                "connectivity_failed": connectivity_failed,
            })
            items.append(metric)
    return items
