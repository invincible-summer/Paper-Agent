"""Administrator paper-platform capability diagnostics.

Every connectivity, search, and abstract check goes through the repository's
official-API backend. Network-paper evidence stops at metadata and valid
non-empty abstracts; uploaded files are the only full-document entry point.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import httpx

from core.config import get_settings
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
    # syntax; the fixed samples validate connectivity and abstract retrieval.
    "openalex": "Attention Is All You Need",
    "semantic_scholar": "Attention Is All You Need",
    "arxiv": "electron",
    "crossref": "10.30554/archmed.21.1.4000.2021",
    "europepmc": "malaria",
    # DOAJ documents fielded path-search syntax.
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
        "rate_policy": "provider_fair_use",
    },
    "medrxiv": {
        "docs": "https://api.biorxiv.org/",
        "endpoint": "https://api.biorxiv.org/details/medrxiv/", "method": "GET",
        "parameters": ("server_path", "start_date_path", "end_date_path", "cursor_path", "format_path"),
        "response_list": "collection", "authentication": "none",
        "rate_policy": "provider_fair_use",
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
}
SLOW_LATENCY_MS = 8000
_RXIV_DIAGNOSTIC_LIMITER = RateLimiter(max_concurrent=1, min_interval=1.0, fast_fail_429=True)
_RXIV_DIAGNOSTIC_DATES = {
    # These are examples published by the official Rxiv API documentation.
    "biorxiv": ("2018-08-21", "2018-08-28", "45"),
    "medrxiv": ("2020-03-21", "2020-03-24", "45"),
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
    return True, "ready"


def source_catalog() -> list[dict]:
    """Return the public metadata/abstract capability catalog only."""
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
            key_configured = bool(getattr(settings, "openaire_client_id", "")) and bool(getattr(settings, "openaire_client_secret", ""))
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
            "operational_status": "ready" if configured and ready else configuration_status if not configured else reason,
            "operational_reason": "ready" if configured and ready else configuration_status if not configured else reason,
            "requires_license_confirmation": bool(spec.requires_license_confirmation),
            "license_confirmation_status": license_status,
            "supports_remote_search": spec.supports_remote_search, "supports_search": True,
            "supports_connectivity": True, "supports_abstract": bool(spec.supports_abstract),
            "official_docs_url": OFFICIAL_PROVIDER_CONTRACTS[source]["docs"],
            "diagnostic_method": "official_api", "local_index_status": local,
        })
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
            papers.append(Paper(
                id=generate_paper_id(title, authors[0] if authors else "", year, doi),
                title=title, authors=authors, year=year, venue=source, doi=doi,
                source=source, abstract=str(item.get("abstract") or "").strip(),
                urls={source: landing},
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




def _validate_sources(sources: list[str] | None) -> list[str]:
    selected = list(SOURCE_IDS) if sources is None else list(dict.fromkeys(sources))
    unknown = [name for name in selected if name not in SOURCE_IDS]
    if unknown:
        raise ValueError(f"未知检测目标：{', '.join(unknown)}")
    return selected


async def diagnose_platform(source: str, capabilities: set[str] | None = None) -> dict:
    if source not in SOURCE_IDS:
        raise ValueError(f"未知检测目标：{source}")
    allowed = {"connectivity", "search", "abstract"}
    if capabilities is not None and not set(capabilities) <= allowed:
        raise ValueError("网络论文全文诊断已永久下线；仅支持 connectivity/search/abstract")
    requested = allowed if capabilities is None else set(capabilities) | {"connectivity"}
    row = {"source": source, "connectivity": _result("not_applicable"),
           "search": _result("not_applicable"), "abstract": _result("not_applicable"),
           "auto_disabled_capabilities": [],
           "_persist_capabilities": sorted({"search", "abstract"} & requested)}
    configured, config_reason = _configuration_status(source)
    if not configured:
        for capability in requested:
            if capability in row:
                row[capability] = _result("not_configured", error_code=config_reason,
                                          message="平台静态配置未满足；不会自动修改管理员开关。")
        return row
    outcome, papers = await _official_search(source)
    if outcome.status == "local_budget_exhausted":
        message = "已达到本进程为官方匿名调用额度保留的安全上限；不会自动修改能力开关。"
        for capability in requested:
            if capability in row:
                row[capability] = _result("not_configured", error_code="local_budget_exhausted", message=message)
        return row
    search = _search_diagnostic(outcome)
    row["connectivity"] = _connectivity_diagnostic(outcome)
    if "search" in requested:
        row["search"] = search
    if "abstract" in requested:
        row["abstract"] = _abstract_diagnostic(source, papers, search)
    if "abstract" in requested and "search" not in requested and search["status"] == "failed":
        row["search"] = search
        row["_persist_capabilities"] = ["search", "abstract"]
    if row["connectivity"]["status"] == "failed":
        row["auto_disabled_capabilities"] = [cap for cap in ("search", "abstract") if cap in row["_persist_capabilities"]]
    else:
        row["auto_disabled_capabilities"] = [cap for cap in row["_persist_capabilities"]
                                              if row[cap].get("status") == "failed"
                                              and row[cap].get("error_code") != "search_prerequisite_failed"]
    return row


async def run_platform_diagnostics(sources: list[str] | None = None, capability: str | None = None) -> list[dict]:
    selected = _validate_sources(sources)
    if capability is not None and capability not in {"connectivity", "search", "abstract"}:
        raise ValueError("网络论文全文诊断已永久下线；仅支持 connectivity/search/abstract")
    requested = {capability} if capability else None
    return await asyncio.gather(*(diagnose_platform(source, requested) for source in selected))


def flatten_capability_diagnostics(platforms: list[dict]) -> list[dict]:
    items: list[dict] = []
    for platform in platforms:
        source = platform["source"]
        connectivity_failed = platform["connectivity"].get("status") == "failed"
        auto = set(platform.get("auto_disabled_capabilities") or [])
        for capability in platform.get("_persist_capabilities", ("search", "abstract")):
            metric = dict(platform.get(capability) or platform["connectivity"])
            if connectivity_failed and capability in auto:
                metric = dict(platform["connectivity"])
                metric["status"] = "failed"
            metric.update({"source": source, "capability": capability,
                           "auto_disable": capability in auto,
                           "connectivity_failed": connectivity_failed})
            items.append(metric)
    return items
