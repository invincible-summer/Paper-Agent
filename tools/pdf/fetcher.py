"""Filename/URL safety helpers for user-file ingestion.

Network-paper PDF discovery and retrieval were retired.  The compatibility ``PDFFetcher`` API intentionally returns no
network files so an old caller cannot re-enable that capability.
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

from core.models import Paper
from core.storage_context import StorageContext

MAX_PDF_BYTES = 50 * 1024 * 1024  # retained for upload parser size contracts


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Validate a public URL for the separately supported user-file importer."""
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"unparseable url: {exc}"
    if parsed.scheme not in {"http", "https"}:
        return False, f"blocked scheme: {parsed.scheme!r}"
    host = (parsed.hostname or "").strip()
    if not host or host.lower() in {"localhost", "metadata.google.internal"}:
        return False, "missing or blocked host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"dns resolution failed for {host}"
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False, "non-IP DNS result"
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return False, f"non-public address {address}"
    return True, ""


def _sanitize_filename_component(raw: str) -> str:
    import re
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", raw or "")
    return cleaned.strip("._")[:120] or "paper"


async def probe_pdf_url(*_args, **_kwargs) -> tuple[bool, str]:
    """Compatibility stub: network-paper probing is permanently disabled."""
    return False, "remote_fulltext_retired"


class PDFFetcher:
    """Compatibility stub that never performs network-paper I/O."""
    def __init__(self, pdf_dir: str | Path = "data/pdfs", *, storage_context: StorageContext | None = None, **_kwargs):
        self.pdf_dir = Path(pdf_dir)
        self.storage_context = storage_context

    async def candidate_urls(self, _paper: Paper) -> list[str]:
        return []

    async def fetch(self, _paper: Paper) -> str | None:
        return None

    async def fetch_many(self, _papers: list[Paper]) -> dict[str, str]:
        return {}

    async def close(self) -> None:
        return None
