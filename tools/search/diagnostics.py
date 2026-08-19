"""Administrator-only paper-source connectivity and bounded PDF speed tests."""
from __future__ import annotations
import asyncio
import time
from urllib.parse import urljoin, urlparse
import httpx
from core.config import get_settings
from core.search_source_health import get_search_health_registry
from tools.pdf.fetcher import PDFFetcher, _is_safe_url, probe_pdf_url
from tools.search.manager import BACKENDS
from tools.search.http_client import get_search_http_client
from tools.search.base import RateLimiter, classify_search_status
from tools.search.registry import SOURCE_IDS, SOURCE_SPECS, contact_email, runtime_gate
from tools.search.rxiv_catalog import sync_status

DIAGNOSTIC_QUERY="retrieval augmented generation survey"
DOWNLOAD_MAX_BYTES=1024*1024
DOWNLOAD_TIMEOUT_SECONDS=20.0
AUXILIARY_TARGETS=("unpaywall","doi")
SOURCE_HOSTS={
    "openalex":"api.openalex.org","semantic_scholar":"api.semanticscholar.org","arxiv":"export.arxiv.org",
    "crossref":"api.crossref.org","europepmc":"www.ebi.ac.uk","doaj":"doaj.org",
    "hal":"api.archives-ouvertes.fr","openaire":"api.openaire.eu","core":"api.core.ac.uk",
    "biorxiv":"api.biorxiv.org","medrxiv":"api.biorxiv.org","pubmed":"eutils.ncbi.nlm.nih.gov",
    "datacite":"api.datacite.org","dblp":"dblp.org",
}
_ALL_TARGETS=set(SOURCE_IDS)|set(AUXILIARY_TARGETS)
_DOWNLOAD_TARGETS={name for name,spec in SOURCE_SPECS.items() if spec.supports_download_test}|{"unpaywall"}
_RXIV_DIAGNOSTIC_LIMITER=RateLimiter(max_concurrent=1,min_interval=1.0,fast_fail_429=True)
_RXIV_DIAGNOSTIC_DATES={"biorxiv":"2013-11-01","medrxiv":"2019-06-01"}


def source_catalog()->list[dict]:
    settings=get_settings().search
    try:
        rxiv=sync_status()
    except Exception:
        rxiv={name:{"server":name,"indexed_count":0,"min_published":None,"max_published":None,
                    "backfill_date":None,"backfill_cursor":0,"last_success_at":None,
                    "last_error":"index_unavailable"} for name in ("biorxiv","medrxiv")}
    rows=[]
    for source,spec in SOURCE_SPECS.items():
        ready,reason=runtime_gate(source,settings)
        local=rxiv.get(source) if source in {"biorxiv","medrxiv"} else None
        if local and not local.get("indexed_count"):
            operational="index_empty"
        else: operational="ready" if ready else reason
        if spec.requires_key:
            configured = bool(getattr(settings, spec.requires_key.lower(), ""))
            configuration_status = "ready" if configured else "missing_api_key"
        elif source == "pubmed":
            configured = bool(contact_email(settings))
            configuration_status = "ready" if configured else "missing_contact_email"
        else:
            configured = True
            configuration_status = "ready"
        if spec.requires_license_confirmation:
            license_confirmed = bool(getattr(settings, spec.requires_license_confirmation.lower(), False))
            license_confirmation_status = "confirmed" if license_confirmed else "not_confirmed"
        else:
            license_confirmation_status = "not_required"
        rows.append({
            "id":source,"display_name":spec.display_name,"coverage":spec.coverage,
            "protocol":spec.protocol,"license_status":spec.license_status,
            "routing_tags":list(spec.routing_tags),"requires_key":bool(spec.requires_key),
            "key_configured":configured,"configuration_status":configuration_status,
            "operational_status":operational,
            "operational_reason":reason if not ready else ("index_empty" if operational == "index_empty" else "ready"),
            "requires_license_confirmation":bool(spec.requires_license_confirmation),
            "license_confirmation_status":license_confirmation_status,
            "supports_remote_search":spec.supports_remote_search,"supports_search":True,
            "supports_connectivity":True,"supports_pdf_probe":spec.supports_pdf_probe,
            "supports_download_test":spec.supports_download_test,"local_index_status":local,
        })
    email=contact_email(settings)
    rows.extend([
        {"id":"unpaywall","display_name":"Unpaywall","coverage":"DOI → OA 全文地址","protocol":"REST API",
         "license_status":"open_api","routing_tags":[],"requires_key":False,"key_configured":bool(email),
         "configuration_status":"ready" if email else "missing_contact_email","operational_status":"ready" if email else "missing_contact_email",
         "operational_reason":"ready" if email else "missing_contact_email","license_confirmation_status":"not_required",
         "requires_license_confirmation":False,"supports_remote_search":False,"supports_search":False,
         "supports_connectivity":True,"supports_pdf_probe":True,"supports_download_test":True,"local_index_status":None},
        {"id":"doi","display_name":"doi.org","coverage":"DOI 重定向解析","protocol":"HTTPS resolver",
         "license_status":"resolver_only","routing_tags":[],"requires_key":False,"key_configured":True,
         "configuration_status":"ready","operational_status":"ready","requires_license_confirmation":False,
         "operational_reason":"ready","license_confirmation_status":"not_required",
         "supports_remote_search":False,"supports_search":False,"supports_connectivity":True,"supports_pdf_probe":True,
         "supports_download_test":False,"local_index_status":None},
    ])
    return rows


def _validate_sources(sources:list[str]|None,*,download:bool=False)->list[str]:
    allowed=_DOWNLOAD_TARGETS if download else _ALL_TARGETS
    default_order=list(SOURCE_IDS)+list(AUXILIARY_TARGETS)
    selected=([name for name in default_order if name in allowed]
              if not sources else list(dict.fromkeys(sources)))
    unknown=sorted(set(selected)-allowed)
    if unknown: raise ValueError(f"未知检测目标：{', '.join(unknown)}")
    return selected


async def _resolve_fixed_endpoint(url:str,*,timeout:float=12.0)->dict:
    current=url
    redirects=0
    requests=0
    async with httpx.AsyncClient(timeout=timeout,follow_redirects=False,proxy=None,trust_env=False) as client:
        for _ in range(6):
            safe,_=_is_safe_url(current)
            if not safe: raise ValueError("unsafe_url")
            async with client.stream("GET",current,headers={"Range":"bytes=0-0"}) as resp:
                requests+=1
                status=int(resp.status_code)
                if status not in {301,302,303,307,308}:
                    return {"status":status,"final_domain":urlparse(current).hostname or "",
                            "redirect_count":redirects,"request_count":requests}
                location=resp.headers.get("location")
                if not location:raise ValueError("redirect_failed")
                current=urljoin(current,location)
                redirects+=1
    raise ValueError("too_many_redirects")


def _note_source_health(items: list[dict]) -> None:
    """Feed real connectivity outcomes into the per-source breaker registry.

    ``note`` semantics: rate_limited/timeout/connection_error/server_error
    count as breaker failures, 2xx as success, everything else is a neutral
    diagnostic. Auxiliary targets (unpaywall/doi) are not retrieval sources
    and are skipped, as are sources never actually probed (not_configured).
    """
    health = get_search_health_registry()
    for item in items:
        source = str(item.get("source") or "")
        if source not in SOURCE_HOSTS or item.get("status") == "not_configured":
            continue
        status = item.get("http_status")
        latency = item.get("latency_ms")
        health.note(
            source,
            status=int(status) if isinstance(status, int) else None,
            latency_ms=int(latency) if isinstance(latency, (int, float)) else None,
            error_code=str(item["error_code"]) if item.get("error_code") else None,
        )


async def run_connectivity(sources:list[str]|None=None)->list[dict]:
    selected=_validate_sources(sources); settings=get_settings().search
    async def one(source:str)->dict:
        started=time.monotonic()
        base={"source":source,"final_domain":SOURCE_HOSTS.get(source, source),"http_status":None,"result_count":0,"pdf_probe_status":"not_run",
              "pdf_probe_latency_ms":None,"request_count":0,"queue_ms":0,"connect_ms":0,"read_ms":0,"network_ms":0,
              "redirect_count":0,"rate_limit_remaining":None,"rate_limit_reset":None}
        if source in AUXILIARY_TARGETS:
            email=contact_email(settings)
            if source=="unpaywall" and not email:return {**base,"status":"not_configured","latency_ms":0,"error_code":"missing_contact_email"}
            try:
                if source=="unpaywall":
                    endpoint="https://api.unpaywall.org/v2/10.1371/journal.pone.0000308"
                    safe,_=_is_safe_url(endpoint)
                    if not safe:raise ValueError("unsafe_url")
                    async with httpx.AsyncClient(timeout=12,follow_redirects=False,proxy=None,trust_env=False) as client:
                        resp=await client.get(endpoint,params={"email":email})
                    status=int(resp.status_code); candidate=None
                    if status==200:
                        payload=resp.json()
                        if not isinstance(payload,dict):
                            return {**base,"status":"schema_mismatch","http_status":status,"request_count":1,
                                    "latency_ms":int((time.monotonic()-started)*1000),"error_code":"schema_mismatch"}
                        best=(payload.get("best_oa_location") or {});candidate=best.get("url_for_pdf") or best.get("url")
                    count=1 if candidate else 0
                    details={"final_domain":resp.url.host,"redirect_count":0,"request_count":1,
                             "rate_limit_remaining":resp.headers.get("x-ratelimit-remaining"),
                             "rate_limit_reset":resp.headers.get("x-ratelimit-reset")}
                else:
                    resolved=await _resolve_fixed_endpoint("https://doi.org/10.1371/journal.pone.0000308")
                    status=int(resolved["status"]);candidate=None;count=1 if status<400 else 0
                    details=resolved
                elapsed_ms=int((time.monotonic()-started)*1000)
                outcome_status,error=classify_search_status(
                    [object()] if status < 400 else [], status,
                    content_type=(resp.headers.get("content-type","") if source=="unpaywall" else ""),
                )
                return {**base,**details,"status":outcome_status,"http_status":status,
                        "latency_ms":elapsed_ms,"network_ms":elapsed_ms,
                        "result_count":count,"error_code":error}
            except httpx.TimeoutException: code="timeout"
            except Exception: code="connection_error"
            return {**base,"status":code,"latency_ms":int((time.monotonic()-started)*1000),"error_code":code}
        if source in {"biorxiv", "medrxiv"}:
            sample_date=_RXIV_DIAGNOSTIC_DATES[source]
            endpoint=f"https://api.biorxiv.org/details/{source}/{sample_date}/{sample_date}/0/json"
            try:
                async with _RXIV_DIAGNOSTIC_LIMITER:
                    resp=await _RXIV_DIAGNOSTIC_LIMITER.fetch(get_search_http_client(),"GET",endpoint)
                status=int(resp.status_code)
                records=(resp.json().get("collection") if status == 200 else None)
                timing=resp.extensions.get("paper_timing",{}) if hasattr(resp,"extensions") else {}
                request_count=int(resp.extensions.get("paper_request_count",1)) if hasattr(resp,"extensions") else 1
                elapsed_ms=int((time.monotonic()-started)*1000)
                queue_ms=int(timing.get("queue_ms",0))
                if status == 200 and not isinstance(records,list):
                    return {**base,"status":"schema_mismatch","http_status":status,"request_count":request_count,
                            "queue_ms":queue_ms,"connect_ms":int(timing.get("connect_ms",0)),"read_ms":int(timing.get("read_ms",0)),
                            "latency_ms":elapsed_ms,"network_ms":max(0,elapsed_ms-queue_ms),
                            "error_code":"schema_mismatch"}
                outcome_status,error=classify_search_status(
                    [object()] if status == 200 else [], status,
                    content_type=resp.headers.get("content-type","") if hasattr(resp,"headers") else "",
                )
                return {**base,"status":outcome_status,"http_status":status,
                        "request_count":request_count,"queue_ms":queue_ms,
                        "connect_ms":int(timing.get("connect_ms",0)),"read_ms":int(timing.get("read_ms",0)),
                        "latency_ms":elapsed_ms,"network_ms":max(0,elapsed_ms-queue_ms),
                        "result_count":len(records or []),"error_code":error}
            except httpx.TimeoutException:
                code="timeout"
            except httpx.RequestError:
                code="connection_error"
            except (ValueError, TypeError):
                code="schema_mismatch"
            return {**base,"status":code,"latency_ms":int((time.monotonic()-started)*1000),"error_code":code}
        ready,reason=runtime_gate(source,settings)
        if not ready:return {**base,"status":"not_configured","latency_ms":0,"error_code":reason}
        try:
            queries=[DIAGNOSTIC_QUERY]
            if source=="arxiv":
                queries=[DIAGNOSTIC_QUERY,"large language model evaluation","agent tool use","multimodal retrieval","long context models","academic search systems"]
            backend=BACKENDS[source]
            if hasattr(backend,"search_many"):
                outcome=await asyncio.wait_for(backend.search_many(queries,5),20.0)
            else:
                papers=await asyncio.wait_for(backend.search(DIAGNOSTIC_QUERY,5),20.0)
                from tools.search.base import SearchOutcome
                outcome=SearchOutcome(source,papers,"ok" if papers else "reachable_empty",request_count=1)
            candidate=next((p.pdf_url for p in outcome.papers if p.pdf_url),None)
            probe="not_run";probe_ms=None
            if candidate:
                ps=time.monotonic();ok,why=await asyncio.wait_for(probe_pdf_url(candidate,timeout=8),10)
                probe_ms=int((time.monotonic()-ps)*1000);probe="ok" if ok else why
            return {**base,"final_domain":outcome.final_domain or base["final_domain"],"status":outcome.status,"http_status":outcome.http_status,
                    "latency_ms":int((time.monotonic()-started)*1000),"network_ms":outcome.network_ms,
                    "request_count":outcome.request_count,"queue_ms":outcome.queue_ms,"connect_ms":outcome.connect_ms,"read_ms":outcome.read_ms,"redirect_count":outcome.redirect_count,
                    "rate_limit_remaining":outcome.rate_limit_remaining,"rate_limit_reset":outcome.rate_limit_reset,
                    "result_count":len(outcome.papers),"pdf_probe_status":probe,"pdf_probe_latency_ms":probe_ms,
                    "error_code":outcome.error_code}
        except asyncio.TimeoutError:
            return {**base,"status":"timeout","latency_ms":int((time.monotonic()-started)*1000),"error_code":"timeout"}
        except Exception:
            return {**base,"status":"connection_error","latency_ms":int((time.monotonic()-started)*1000),"error_code":"connection_error"}
    items = await asyncio.gather(*(one(source) for source in selected))
    _note_source_health(items)
    return items

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
