"""Controlled arXiv PDF access through the documented public PDF URL.

The production network has historically required a browser-like client for
arXiv.  Every arXiv probe, diagnostic and real download uses this one adapter,
invoking wget with an argv array (never a shell) and ``User-Agent: Lynx``.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from tools.pdf.fetcher import _is_safe_url

_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
_MAX_ARXIV_PDF_BYTES = 50 * 1024 * 1024
_PROCESS_POLL_SECONDS = 0.05
_ARXIV_PDF_RE = re.compile(
    r"^/pdf/(?:[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)(?:/[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)*(?:\.pdf)?$"
)


@dataclass(frozen=True)
class ArxivWgetResult:
    ok: bool
    status: str
    error_code: str | None
    elapsed_ms: int
    bytes_read: int
    kb_per_second: float
    exit_code: int | None
    pdf_magic_valid: bool
    path: str | None = None


def _validate_arxiv_url(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _ARXIV_HOSTS:
        return False, "arxiv_url_not_allowed"
    # Decode once before validating so encoded traversal (``%2e%2e``) cannot
    # reach wget.  arXiv IDs may contain one or more category path segments,
    # but no dot segments, backslashes, or query/fragment payloads.
    path = unquote(parsed.path or "")
    if parsed.query or parsed.fragment or "\x00" in path:
        return False, "arxiv_pdf_path_invalid"
    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments[2:]):
        return False, "arxiv_pdf_path_invalid"
    if "\\" in path or not _ARXIV_PDF_RE.fullmatch(path):
        return False, "arxiv_pdf_path_invalid"
    safe, reason = _is_safe_url(url)
    return (True, "") if safe else (False, f"unsafe_url:{reason}")


async def _stop_process(process) -> None:
    """Terminate a wget process, escalating to kill after a bounded grace period."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _wait_for_wget(process, temp_path: Path, *, timeout: float, max_bytes: int) -> str | None:
    """Wait while enforcing both wall-clock and on-disk byte limits."""
    wait_task = asyncio.create_task(process.wait())
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while not wait_task.done():
            if temp_path.exists() and temp_path.stat().st_size > max_bytes:
                await _stop_process(process)
                return "file_too_large"
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await _stop_process(process)
                return "timeout"
            await asyncio.wait({wait_task}, timeout=min(_PROCESS_POLL_SECONDS, remaining))
        await wait_task
        return None
    finally:
        if not wait_task.done():
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)


async def fetch_arxiv_pdf(
    url: str, *, max_bytes: int, timeout: float, destination: str | Path | None = None,
    max_redirects: int = 5, probe: bool = False,
) -> ArxivWgetResult:
    allowed, reason = _validate_arxiv_url(url)
    if not allowed:
        return ArxivWgetResult(False, "failed", reason, 0, 0, 0.0, None, False)
    try:
        max_bytes = min(_MAX_ARXIV_PDF_BYTES, max(5, int(max_bytes)))
        timeout = min(120.0, max(1.0, float(timeout)))
        max_redirects = min(10, max(0, int(max_redirects)))
    except (TypeError, ValueError):
        return ArxivWgetResult(False, "failed", "invalid_limits", 0, 0, 0.0, None, False)
    started = time.monotonic()
    dest = Path(destination) if destination is not None else None
    temp_parent = dest.parent if dest is not None else None
    fd, name = tempfile.mkstemp(prefix="arxiv-wget-", suffix=".pdf.part", dir=temp_parent)
    os.close(fd)
    temp_path = Path(name)
    process = None
    exit_code = None
    error_code = None
    try:
        argv = [
            "wget", "--user-agent=Lynx", f"--timeout={max(1, int(timeout))}",
            "--tries=1", f"--max-redirect={max_redirects}",
            "--no-proxy", f"--quota={max_bytes}",
            *([f"--header=Range: bytes=0-{max_bytes - 1}"] if probe else []),
            "-O", str(temp_path), url,
        ]
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        error_code = await _wait_for_wget(
            process, temp_path, timeout=timeout + 1.0, max_bytes=max_bytes
        )
        exit_code = process.returncode
        size = temp_path.stat().st_size if temp_path.exists() else 0
        if size > max_bytes:
            error_code = "file_too_large"
        magic = b""
        if temp_path.exists():
            with temp_path.open("rb") as stream:
                magic = stream.read(5)
        pdf_valid = magic == b"%PDF-"
        if error_code is None and exit_code != 0 and not (probe and pdf_valid):
            error_code = "wget_exit_nonzero"
        if error_code is None and not pdf_valid:
            error_code = "not_pdf"
        elapsed = max(0.001, time.monotonic() - started)
        if error_code is None and dest is not None:
            os.replace(temp_path, dest)
            temp_path = dest
        return ArxivWgetResult(
            error_code is None, "ok" if error_code is None else "failed", error_code,
            int(elapsed * 1000), size, round(size / 1024 / elapsed, 1), exit_code,
            pdf_valid, str(temp_path) if error_code is None and dest is not None else None,
        )
    except FileNotFoundError:
        elapsed = max(0.001, time.monotonic() - started)
        return ArxivWgetResult(False, "failed", "wget_not_installed", int(elapsed * 1000), 0, 0.0, None, False)
    except Exception as exc:  # noqa: BLE001
        elapsed = max(0.001, time.monotonic() - started)
        return ArxivWgetResult(False, "failed", type(exc).__name__.lower(), int(elapsed * 1000), 0, 0.0, exit_code, False)
    finally:
        if temp_path.exists() and (dest is None or temp_path != dest):
            temp_path.unlink(missing_ok=True)
