"""PDF fetcher: download OA full text (DESIGN D-012).

Candidate order: the source-API pdf_url (arXiv / OpenAlex best-OA / Europe PMC
OA-only / DOAJ) -> Unpaywall best_oa_location (DOI lookup, free, email only).
OA-only by policy: paywalled publisher links are never followed. Falls back to
abstract-only if no OA PDF is available.

``probe_pdf_url`` is the lightweight sibling used by search_papers /
research_map: it reads only the first few KB to verify the PDF magic without
downloading the file.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from urllib.parse import urljoin

import httpx

from core.models import Paper
from core.storage_context import StorageContext

logger = logging.getLogger(__name__)


# Security (D-089): cap download size and reject unsafe URLs (SSRF guard).
# pdf_url comes from external API responses (OpenAlex/arXiv/etc.); without
# validation a malicious or compromised record could point the backend at
# internal services (cloud metadata 169.254.169.254), loopback, or a non-http
# scheme (file://). The size cap prevents disk/memory exhaustion.
MAX_PDF_BYTES = 50 * 1024 * 1024  # 50 MB hard cap

# Process-global download politeness: ALL users of a deployment share this one
# limiter, so upstream hosts (arXiv, repositories, ...) never see per-user
# bursts — 2 concurrent downloads, at least 1s between starts.
from tools.search.base import RateLimiter  # noqa: E402

_download_limiter = RateLimiter(max_concurrent=2, min_interval=1.0)
# Lightweight availability probes (a few KB at most, not full downloads).
_probe_limiter = RateLimiter(max_concurrent=4, min_interval=0.25)


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Validate a URL for safe server-side fetching (SSRF guard, D-089).

    Returns (ok, reason). ok=False means the URL must NOT be fetched.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"unparseable url: {e}"
    if parsed.scheme not in ("http", "https"):
        return False, f"blocked scheme: {parsed.scheme!r}"
    host = (parsed.hostname or "").strip()
    if not host:
        return False, "missing host"
    if host.lower() in {"localhost", "metadata.google.internal"}:
        return False, f"blocked host: {host}"

    # Resolve and inspect every returned address; reject anything non-public.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"dns resolution failed for {host}"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, f"non-IP resolved for {host}: {addr}"
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, f"non-public address {ip} for {host}"
        if str(ip).startswith("169.254."):
            return False, f"metadata endpoint {ip} for {host}"
    return True, ""


def _sanitize_filename_component(raw: str) -> str:
    """Make `raw` safe to embed in a filename under pdf_dir (D-089).

    paper.id is normally doi:<doi> or hash:<hex>, but it originates from
    external records; restrict to a conservative charset so it can never
    carry path separators, `..`, or NULs.
    """
    import re
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
    cleaned = cleaned.strip("._") or "paper"
    return cleaned[:120]


async def probe_pdf_url(
    url: str, *, timeout: float = 20.0, max_redirects: int = 5,
) -> tuple[bool, str]:
    """Probe whether ``url`` is a live PDF — WITHOUT downloading the file.

    Streams only the first few KB (Range request where supported), follows
    redirects with the same per-hop SSRF guard as the full downloader, and
    returns ``(True, "ok")`` only when the body starts with ``%PDF-``.

    This is the availability signal used by search_papers / research_map.
    A paywalled landing page (HTML) is therefore never labelled available.
    The actual full download only happens later in deep_read.
    """
    import asyncio

    current = url
    use_range = True
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                     proxy=None, trust_env=False) as client:
            for _hop in range(max_redirects + 1):
                safe, reason = _is_safe_url(current)
                if not safe:
                    return False, f"unsafe:{reason}"
                headers = {"Range": "bytes=0-8191"} if use_range else None
                next_url: str | None = None
                retry_without_range = False
                status = 0
                magic = b""
                try:
                    async with _probe_limiter:
                        async with client.stream("GET", current, headers=headers) as resp:
                            if resp.status_code in {301, 302, 303, 307, 308}:
                                location = resp.headers.get("location")
                                next_url = urljoin(current, location) if location else None
                                if next_url is None:
                                    return False, "redirect_failed"
                            elif resp.status_code not in (200, 206):
                                status = resp.status_code
                            else:
                                async for chunk in resp.aiter_bytes():
                                    if not chunk:
                                        continue
                                    magic += chunk
                                    if len(magic) >= 5:
                                        break
                except asyncio.TimeoutError:
                    return False, "timeout"
                except Exception as e:  # noqa: BLE001
                    logger.debug("pdf probe network error for %s: %s", url, e)
                    return False, "network_error"

                if next_url:
                    current = next_url
                    continue
                if status:
                    # Some OA servers reject Range requests even though a plain
                    # GET would stream the PDF. Retry once without Range so the
                    # probe does not turn a server quirk into a false negative.
                    if use_range and status in {400, 405, 416, 501}:
                        use_range = False
                        retry_without_range = True
                    else:
                        return False, f"http_{status}"
                if retry_without_range:
                    continue
                return (True, "ok") if magic.startswith(b"%PDF-") else (False, "not_pdf")
        return False, "redirect_failed"
    except Exception as e:  # noqa: BLE001
        logger.debug("pdf probe failed for %s: %s", url, e)
        return False, "probe_error"


class PDFFetcher:
    def __init__(self, pdf_dir: str = "data/pdfs",
                 storage_context: StorageContext | None = None):
        self.storage_context = storage_context
        self._artifact_store = None
        if storage_context is not None and storage_context.channel == "openai_api":
            from core.api_artifact_store import ApiArtifactStore
            from core.api_storage_store import ApiStorageStore
            storage_context.ensure_layout()
            self._artifact_store = ApiArtifactStore(ApiStorageStore(storage_context))
            self.pdf_dir = storage_context.temp_dir
        else:
            self.pdf_dir = Path(pdf_dir)
            self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False)

    async def candidate_urls(self, paper: Paper) -> list[str]:
        """OA PDF candidates in fetch order (no download, Unpaywall included)."""
        urls: list[str] = []
        if paper.pdf_url:
            urls.append(paper.pdf_url)
        oa_url = await self._unpaywall_pdf_url(paper.doi) if paper.doi else None
        if oa_url and oa_url not in urls:
            urls.append(oa_url)
        return urls

    async def fetch(self, paper: Paper) -> str | None:
        """Download an OA PDF for a paper. Returns local path or None.

        Tries the source-API pdf_url first, then Unpaywall's best OA location
        (DOI lookup). Every candidate passes the SSRF guard and the PDF
        content check, so a paywalled landing page never gets saved.
        """
        urls = await self.candidate_urls(paper)
        if not urls:
            logger.debug("No OA pdf candidate for paper: %s", paper.id)
            return None

        logical_name = f"{_sanitize_filename_component(paper.id)}.pdf"
        if getattr(self, "_artifact_store", None) is not None:
            existing = self._artifact_store.find_active(
                category="public_pdf", scope="public", logical_name=logical_name
            )
            if existing is not None:
                return str(existing.path)
        local_path = self.pdf_dir / logical_name
        if local_path.exists():
            logger.debug("PDF already cached: %s", local_path)
            return str(local_path)

        for url in urls:
            path = await self._download(url, local_path, paper.title)
            if path:
                return path
        return None

    async def _unpaywall_pdf_url(self, doi: str) -> str | None:
        """Best OA PDF url for a DOI via Unpaywall (free, legal OA copies only).

        Reuses the polite-pool email already configured for OpenAlex/Crossref;
        returns None when no email is configured or no OA location exists.
        """
        from core.config import get_settings

        email = (get_settings().search.openalex_email
                 or get_settings().search.crossref_email or "")
        doi = (doi or "").strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if doi.lower().startswith(prefix):
                doi = doi[len(prefix):]
        if not doi or not email:
            return None
        try:
            resp = await self._client.get(
                f"https://api.unpaywall.org/v2/{doi}", params={"email": email})
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data.get("is_oa"):
                return None
            best = data.get("best_oa_location") or {}
            url = best.get("url_for_pdf") or best.get("url")
            if url:
                return url
            for loc in data.get("oa_locations") or []:
                url = loc.get("url_for_pdf")
                if url:
                    return url
        except Exception as e:  # noqa: BLE001
            logger.debug("unpaywall lookup failed for %s: %s", doi, e)
        return None

    async def _download(self, url: str, local_path: Path, title: str = "") -> str | None:
        """Stream one OA PDF with per-redirect SSRF validation."""
        from tools.ingest.downloader import download_to_temp

        temp_dir = (
            self.storage_context.temp_dir
            if getattr(self, "storage_context", None) is not None
            and self.storage_context.channel == "openai_api"
            else local_path.parent
        )
        temp_path = None
        try:
            async with _download_limiter:
                temp_path, content_type = await download_to_temp(
                    url, temp_dir=temp_dir, max_bytes=MAX_PDF_BYTES
                )
            with temp_path.open("rb") as stream:
                magic = stream.read(5)
            if "pdf" not in (content_type or "").lower() and magic != b"%PDF-":
                logger.warning("Downloaded OA candidate is not a PDF")
                return None
            if getattr(self, "_artifact_store", None) is not None:
                artifact = self._artifact_store.save_public_pdf(
                    temp_path, logical_name=local_path.name
                )
                return str(artifact.path)
            os.replace(temp_path, local_path)
            temp_path = None
            logger.info("Downloaded OA PDF: %s -> %s", title[:50], local_path.name)
            return str(local_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PDF download failed: %s", type(exc).__name__)
            return None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    async def fetch_many(self, papers: list[Paper]) -> dict[str, str]:
        """Download PDFs for multiple papers concurrently.

        Returns dict of paper_id -> local_path (only successful downloads).
        """
        import asyncio
        tasks = []
        for paper in papers:
            tasks.append(self._fetch_with_id(paper))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {pid: path for pid, path in results if path}

    async def _fetch_with_id(self, paper: Paper) -> tuple[str, str | None]:
        path = await self.fetch(paper)
        return (paper.id, path)

    async def close(self):
        await self._client.aclose()
