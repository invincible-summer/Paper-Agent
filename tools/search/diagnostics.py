"""Administrator-only paper-source connectivity and bounded PDF speed tests."""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urljoin, urlparse

import httpx

from core.config import get_settings
from core.paper_search_settings_store import SOURCE_IDS
from core.search_source_health import get_search_health_registry
from tools.pdf.fetcher import PDFFetcher, _is_safe_url, probe_pdf_url
from tools.search.manager import BACKENDS

DIAGNOSTIC_QUERY = "retrieval augmented generation survey"
DOWNLOAD_MAX_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 20.0
AUXILIARY_TARGETS = ("unpaywall", "doi")
_ALL_TARGETS = set(SOURCE_IDS) | set(AUXILIARY_TARGETS)
_DOWNLOAD_TARGETS = _ALL_TARGETS


def source_catalog() -> list[dict]:
    settings = get_settings()
    configured = {
        "semantic_scholar": bool(settings.search.s2_api_key),
        "core": bool(settings.search.core_api_key),
        "openalex": bool(settings.search.openalex_email),
        "crossref": bool(settings.search.crossref_email),
    }
    labels = {
        "openalex": ("OpenAlex", "综合学科元数据与引用"),
        "semantic_scholar": ("Semantic Scholar", "引用、作者与计算机科学覆盖"),
        "arxiv": ("arXiv", "预印本与直接 OA PDF"),
        "crossref": ("Crossref", "跨学科 DOI 元数据"),
        "europepmc": ("Europe PMC", "生物医学与生命科学"),
        "doaj": ("DOAJ", "开放获取期刊"),
        "hal": ("HAL", "欧洲开放仓储与人文社科"),
        "openaire": ("OpenAIRE", "欧洲开放研究图谱"),
        "core": ("CORE", "开放仓储全文聚合"),
    }
    requires = {"semantic_scholar": "S2_API_KEY", "core": "CORE_API_KEY"}
    config_status = {
        "openalex": "Polite email 已配置" if settings.search.openalex_email else "Polite email 未配置",
        "crossref": "Polite email 已配置" if settings.search.crossref_email else "Polite email 未配置",
        "semantic_scholar": "API key 已配置" if settings.search.s2_api_key else "API key 未配置（公共额度）",
        "core": "API key 已配置" if settings.search.core_api_key else "API key 未配置",
    }
    return [
        {"id": source, "display_name": labels[source][0], "coverage": labels[source][1],
         "requires_key": source in requires, "key_configured": configured.get(source, True),
         "configuration_status": config_status.get(source, "无需额外配置"),
         "supports_search": True, "supports_connectivity": True,
         "supports_pdf_probe": True, "supports_download_test": True}
        for source in SOURCE_IDS
    ] + [
        {"id": "unpaywall", "display_name": "Unpaywall", "coverage": "DOI → OA 全文地址",
         "requires_key": False,
         "key_configured": bool(settings.search.openalex_email or settings.search.crossref_email),
         "configuration_status": "Email 已配置" if (settings.search.openalex_email or settings.search.crossref_email) else "Email 未配置",
         "supports_search": False, "supports_connectivity": True,
         "supports_pdf_probe": True, "supports_download_test": True},
        {"id": "doi", "display_name": "doi.org", "coverage": "DOI 重定向解析",
         "requires_key": False, "key_configured": True, "configuration_status": "无需额外配置",
         "supports_search": False, "supports_connectivity": True,
         "supports_pdf_probe": True, "supports_download_test": True},
    ]


def _validate_sources(sources: list[str] | None, *, download: bool = False) -> list[str]:
    allowed = _DOWNLOAD_TARGETS if download else _ALL_TARGETS
    default_order = list(SOURCE_IDS) + list(AUXILIARY_TARGETS)
    selected = default_order if not sources else list(dict.fromkeys(sources))
    unknown = sorted(set(selected) - allowed)
    if unknown:
        raise ValueError(f"未知检测目标：{', '.join(unknown)}")
    return selected


async def _resolve_fixed_endpoint(url: str, *, timeout: float = 12.0) -> int:
    """Resolve a fixed diagnostic endpoint with an SSRF check on every hop."""
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 proxy=None, trust_env=False) as client:
        for _ in range(6):
            safe, _reason = _is_safe_url(current)
            if not safe:
                raise ValueError("unsafe_url")
            async with client.stream("GET", current, headers={"Range": "bytes=0-0"}) as resp:
                status = int(resp.status_code)
                if status not in {301, 302, 303, 307, 308}:
                    return status
                location = resp.headers.get("location")
                if not location:
                    raise ValueError("redirect_failed")
                current = urljoin(current, location)
    raise ValueError("too_many_redirects")


async def run_connectivity(sources: list[str] | None = None) -> list[dict]:
    selected = _validate_sources(sources)
    settings = get_settings()
    health = get_search_health_registry()

    async def one(source: str) -> dict:
        if source in AUXILIARY_TARGETS:
            started = time.monotonic()
            email = settings.search.openalex_email or settings.search.crossref_email
            if source == "unpaywall" and not email:
                return {"source": source, "status": "not_configured", "http_status": None,
                        "latency_ms": 0, "result_count": 0, "pdf_probe_status": "not_run",
                        "pdf_probe_latency_ms": None, "error_code": "missing_email"}
            try:
                if source == "unpaywall":
                    endpoint = "https://api.unpaywall.org/v2/10.1371/journal.pone.0000308"
                    safe, _reason = _is_safe_url(endpoint)
                    if not safe:
                        raise ValueError("unsafe_url")
                    async with httpx.AsyncClient(timeout=12, follow_redirects=False,
                                                 proxy=None, trust_env=False) as client:
                        resp = await client.get(endpoint, params={"email": email})
                    http_status = int(resp.status_code)
                    candidate = None
                    if http_status == 200:
                        data = resp.json()
                        best = data.get("best_oa_location") or {}
                        candidate = best.get("url_for_pdf") or best.get("url")
                    result_count = 1 if candidate else 0
                else:
                    http_status = await _resolve_fixed_endpoint(
                        "https://doi.org/10.1371/journal.pone.0000308")
                    candidate = None
                    result_count = 1 if http_status < 400 else 0
                probe_status = "not_run"
                probe_ms = None
                if candidate:
                    probe_started = time.monotonic()
                    ok, reason = await asyncio.wait_for(
                        probe_pdf_url(candidate, timeout=8), 10)
                    probe_ms = int((time.monotonic() - probe_started) * 1000)
                    probe_status = "ok" if ok else reason
                status = "ok" if http_status < 400 else "http_error"
                return {"source": source, "status": status,
                        "http_status": http_status,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "result_count": result_count, "pdf_probe_status": probe_status,
                        "pdf_probe_latency_ms": probe_ms,
                        "error_code": None if status == "ok" else "http_error"}
            except httpx.TimeoutException:
                code = "timeout"
            except Exception:
                code = "connection_error"
            return {"source": source, "status": code, "http_status": None,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "result_count": 0, "pdf_probe_status": "not_run",
                    "pdf_probe_latency_ms": None, "error_code": code}
        if source == "core" and not settings.search.core_api_key:
            return {"source": source, "status": "not_configured", "http_status": None,
                    "latency_ms": 0, "result_count": 0, "pdf_probe_status": "not_run",
                    "pdf_probe_latency_ms": None, "error_code": "missing_api_key"}
        started = time.monotonic()
        try:
            papers = await asyncio.wait_for(BACKENDS[source].search(DIAGNOSTIC_QUERY, 5), 20.0)
            latency = int((time.monotonic() - started) * 1000)
            item = health.get(source)
            http_status = item.get("last_http_status")
            error = item.get("last_error_code")
            if http_status == 429 or error == "rate_limited":
                status = "rate_limited"
            elif error in {"connection_error", "timeout", "server_error", "invalid_response"}:
                status = error
            elif http_status and http_status >= 400:
                status = "http_error"
            else:
                status = "ok" if papers else "reachable_empty"
            probe_status = "not_run"
            probe_ms = None
            candidate = next((paper.pdf_url for paper in papers if paper.pdf_url), None)
            if candidate:
                probe_started = time.monotonic()
                ok, reason = await asyncio.wait_for(probe_pdf_url(candidate, timeout=8), 10)
                probe_ms = int((time.monotonic() - probe_started) * 1000)
                probe_status = "ok" if ok else reason
            return {"source": source, "status": status, "http_status": http_status,
                    "latency_ms": latency, "result_count": len(papers),
                    "pdf_probe_status": probe_status, "pdf_probe_latency_ms": probe_ms,
                    "error_code": error}
        except asyncio.TimeoutError:
            health.record_failure(source, "timeout")
            return {"source": source, "status": "timeout", "http_status": None,
                    "latency_ms": int((time.monotonic() - started) * 1000), "result_count": 0,
                    "pdf_probe_status": "not_run", "pdf_probe_latency_ms": None,
                    "error_code": "timeout"}
        except Exception:
            health.record_failure(source, "connection_error")
            return {"source": source, "status": "connection_error", "http_status": None,
                    "latency_ms": int((time.monotonic() - started) * 1000), "result_count": 0,
                    "pdf_probe_status": "not_run", "pdf_probe_latency_ms": None,
                    "error_code": "connection_error"}

    return await asyncio.gather(*(one(source) for source in selected))


async def _candidate_for_target(target: str) -> str | None:
    if target == "doi":
        return "https://doi.org/10.1371/journal.pone.0000308"
    if target == "unpaywall":
        fetcher = PDFFetcher("/tmp", admin_override=True)
        try:
            return await fetcher._unpaywall_pdf_url("10.1371/journal.pone.0000308")
        finally:
            await fetcher.close()
    if target == "arxiv":
        return "https://arxiv.org/pdf/1706.03762"
    papers = await asyncio.wait_for(BACKENDS[target].search(DIAGNOSTIC_QUERY, 5), 20.0)
    for paper in papers:
        fetcher = PDFFetcher("/tmp", admin_override=True)
        try:
            urls = await fetcher.candidate_urls(paper)
        finally:
            await fetcher.close()
        if urls:
            return urls[0]
    return None


async def _speed_test_url(target: str, url: str) -> dict:
    started = time.monotonic()
    current = url
    redirects = 0
    total = 0
    magic = b""
    http_status = None
    try:
        timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                     proxy=None, trust_env=False) as client:
            while redirects <= 5:
                safe, _reason = _is_safe_url(current)
                if not safe:
                    raise ValueError("unsafe_url")
                async with client.stream("GET", current,
                                         headers={"Range": f"bytes=0-{DOWNLOAD_MAX_BYTES - 1}"}) as resp:
                    http_status = int(resp.status_code)
                    if http_status in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("location")
                        if not location:
                            raise ValueError("redirect_failed")
                        current = urljoin(current, location)
                        redirects += 1
                        continue
                    if http_status not in {200, 206}:
                        raise ValueError(f"http_{http_status}")
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        if len(magic) < 5:
                            magic += chunk[:5 - len(magic)]
                        remaining = DOWNLOAD_MAX_BYTES - total
                        total += min(len(chunk), remaining)
                        if total >= DOWNLOAD_MAX_BYTES:
                            break
                break
        elapsed = max(0.001, time.monotonic() - started)
        return {"target": target, "status": "ok" if magic.startswith(b"%PDF-") else "not_pdf",
                "domain": urlparse(current).hostname or "", "http_status": http_status,
                "bytes_read": total, "elapsed_ms": int(elapsed * 1000),
                "kb_per_second": round(total / 1024 / elapsed, 1),
                "pdf_magic_valid": magic.startswith(b"%PDF-"), "redirects": redirects,
                "error_code": None if magic.startswith(b"%PDF-") else "not_pdf"}
    except asyncio.TimeoutError:
        code = "timeout"
    except Exception as exc:
        code = str(exc) if str(exc) else type(exc).__name__
    elapsed = max(0.001, time.monotonic() - started)
    return {"target": target, "status": "failed", "domain": urlparse(current).hostname or "",
            "http_status": http_status, "bytes_read": total, "elapsed_ms": int(elapsed * 1000),
            "kb_per_second": round(total / 1024 / elapsed, 1),
            "pdf_magic_valid": False, "redirects": redirects, "error_code": code[:80]}


async def run_download_speed(sources: list[str] | None = None) -> list[dict]:
    selected = _validate_sources(sources, download=True)

    async def one(target: str) -> dict:
        try:
            url = await _candidate_for_target(target)
        except asyncio.TimeoutError:
            url = None
        except Exception:
            url = None
        if not url:
            return {"target": target, "status": "no_candidate", "domain": "",
                    "http_status": None, "bytes_read": 0, "elapsed_ms": 0,
                    "kb_per_second": 0.0, "pdf_magic_valid": False,
                    "redirects": 0, "error_code": "no_oa_candidate"}
        try:
            return await asyncio.wait_for(_speed_test_url(target, url), DOWNLOAD_TIMEOUT_SECONDS + 2)
        except asyncio.TimeoutError:
            return {"target": target, "status": "failed", "domain": "",
                    "http_status": None, "bytes_read": 0, "elapsed_ms": int((DOWNLOAD_TIMEOUT_SECONDS + 2) * 1000),
                    "kb_per_second": 0.0, "pdf_magic_valid": False,
                    "redirects": 0, "error_code": "timeout"}

    return await asyncio.gather(*(one(target) for target in selected))
