"""OA full-text availability probe (lightweight, no full download).

``Paper.fulltext_status`` has exactly three states:

  available    a live OA PDF was reachable (first bytes are ``%PDF-``), or a
               local PDF / verified deep_read result already exists
  unavailable  every OA candidate was tried and none is a live PDF
  unknown      not verified yet (must never be displayed as "可获取")

This module is used by search_papers and research_map. It deliberately does
NOT download the PDF file: a genealogy/search card only needs to know whether
the PDF is reachable. The actual full download + parse happens later inside
deep_read, and its outcome is written back into the same status field.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from pathlib import Path
from typing import Callable

from core.config import get_settings
from core.models import Paper
from core.reading_policy import (
    FULLTEXT_STATUS_AVAILABLE,
    FULLTEXT_STATUS_UNAVAILABLE,
    FULLTEXT_STATUS_UNKNOWN,
    normalize_fulltext_status,
)
from tools.pdf.fetcher import PDFFetcher, probe_pdf_url
from tools.storage.database import Database

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 20.0
_DEFAULT_TOTAL_TIMEOUT_SECONDS = 180.0
_DEFAULT_TTL_DAYS = 30

ProgressCallback = Callable[[str], None]

# Probe reasons that are definitive (the URL was reached and is not a PDF).
# Everything else (timeout / network_error / stream_error / probe_error) is
# transient and leaves the paper ``unknown`` rather than asserting a negative.
_DEFINITIVE_NEGATIVE_REASONS = {"not_pdf", "redirect_failed", "unsafe"}


def _checked_at_is_fresh(checked_at: str | None, ttl_days: int) -> bool:
    if not checked_at:
        return False
    try:
        stamp = datetime.datetime.fromisoformat(checked_at)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    age = datetime.datetime.now(datetime.timezone.utc) - stamp
    return age.total_seconds() >= 0 and age.days < ttl_days


def _cached_row_applies(row: dict, paper: Paper) -> bool:
    """A cached verification is reusable only when its inputs still hold."""
    status = normalize_fulltext_status(row.get("status"))
    pdf_path = row.get("pdf_path") or ""
    if status == FULLTEXT_STATUS_AVAILABLE:
        # Actual-download rows are keyed to the local file; probe-only rows are
        # keyed to the candidate URL that was successfully probed.
        if pdf_path:
            return Path(pdf_path).is_file()
        return (row.get("candidate_url") or "") == (paper.pdf_url or "")
    if status == FULLTEXT_STATUS_UNAVAILABLE:
        old_url = row.get("candidate_url") or ""
        new_url = paper.pdf_url or ""
        return old_url == new_url
    return False


def _local_pdf_is_ready(pdf_path: str | None) -> bool:
    """A cached local PDF counts as available without touching the network."""
    if not pdf_path:
        return False
    try:
        with Path(pdf_path).open("rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


async def _check_one(
    paper: Paper,
    fetcher: PDFFetcher,
    *,
    storage_context=None,
) -> tuple[str, str, str, str]:
    """Return (status, evidence, pdf_path, candidate_url) for one paper."""
    candidate_url = paper.pdf_url or ""
    local_path = paper.pdf_path or ""
    if _local_pdf_is_ready(local_path):
        return (FULLTEXT_STATUS_AVAILABLE, "local_pdf_cached", local_path, candidate_url)

    try:
        urls = await asyncio.wait_for(
            fetcher.candidate_urls(paper), timeout=_PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return (FULLTEXT_STATUS_UNKNOWN, "candidate_lookup_timeout", "", candidate_url)
    except Exception as e:  # noqa: BLE001
        logger.debug("candidate lookup failed for %s: %s", paper.id, e)
        return (FULLTEXT_STATUS_UNKNOWN, "candidate_lookup_error", "", candidate_url)

    if not urls:
        # No OA candidate at all (metadata + Unpaywall). This is a verified
        # negative for the current record.
        return (FULLTEXT_STATUS_UNAVAILABLE, "no_oa_url", "", candidate_url)

    reasons: list[str] = []
    transient_seen = False
    for url in urls:
        try:
            ok, reason = await asyncio.wait_for(
                probe_pdf_url(url), timeout=_PROBE_TIMEOUT_SECONDS + 5
            )
        except asyncio.TimeoutError:
            transient_seen = True
            reasons.append("timeout")
            continue
        except Exception as e:  # noqa: BLE001
            logger.debug("pdf probe failed for %s (%s): %s", paper.id, url, e)
            transient_seen = True
            reasons.append("probe_error")
            continue
        if ok:
            return (FULLTEXT_STATUS_AVAILABLE, f"oa_url_probe_verified:{url}",
                    "", candidate_url)
        reasons.append(reason)
        if reason.startswith("http_") and reason not in {
            "http_408", "http_425", "http_429", "http_500",
            "http_502", "http_503", "http_504",
        }:
            # Reached the server and it clearly does not serve this URL as a
            # PDF (403/404/HTML landing page, ...) — a real negative.
            continue
        if reason in _DEFINITIVE_NEGATIVE_REASONS:
            continue
        transient_seen = True

    summary = ";".join(dict.fromkeys(reasons))[:200]
    if transient_seen:
        return (FULLTEXT_STATUS_UNKNOWN, f"probe_transient_error:{summary}",
                "", candidate_url)
    return (FULLTEXT_STATUS_UNAVAILABLE, f"oa_url_probe_failed:{summary}",
            "", candidate_url)


async def verify_papers_fulltext(
    papers: list[Paper],
    *,
    storage_context=None,
    progress_callback: ProgressCallback | None = None,
    timeout_seconds: float | None = None,
    ttl_days: int = _DEFAULT_TTL_DAYS,
) -> dict[str, str]:
    """Probe OA PDF availability for a paper set and persist the results.

    No PDF body is downloaded here. Returns ``{paper_id: fulltext_status}``.
    Papers not probed within the total deadline keep ``unknown`` — callers and
    the frontend must render that as "待验证", never as "可获取".
    """
    if not papers:
        return {}

    s = get_settings()
    if timeout_seconds is None:
        timeout_seconds = float(
            getattr(getattr(s, "search", None), "fulltext_verify_timeout_seconds", None)
            or _DEFAULT_TOTAL_TIMEOUT_SECONDS
        )
    if ttl_days is None:
        ttl_days = int(
            getattr(getattr(s, "search", None), "fulltext_status_ttl_days", None)
            or _DEFAULT_TTL_DAYS
        )

    def report(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    db = Database(storage_context=storage_context) if storage_context is not None \
        else Database(s.storage.sqlite_path)
    statuses: dict[str, str] = {}
    to_check: list[Paper] = []
    try:
        cached = db.get_fulltext_statuses([p.id for p in papers])
        for paper in papers:
            row = cached.get(paper.id)
            if row and _checked_at_is_fresh(row.get("checked_at"), ttl_days) \
                    and _cached_row_applies(row, paper):
                status = normalize_fulltext_status(row.get("status"))
                paper.fulltext_status = status
                if status == FULLTEXT_STATUS_AVAILABLE:
                    paper.pdf_path = row.get("pdf_path") or paper.pdf_path
                statuses[paper.id] = status
            else:
                to_check.append(paper)

        if not to_check:
            report("全文可获取性探测：全部复用已核实缓存")
            return statuses

        report(f"全文可获取性探测 0/{len(to_check)}（只读取 PDF 文件头，不下载全文）")
        fetcher = PDFFetcher(s.reader.pdf_dir, storage_context=storage_context)
        rows_to_save: list[tuple[str, str, str, str, str]] = []

        async def _run_one(paper: Paper) -> None:
            status, evidence, pdf_path, candidate_url = await _check_one(
                paper, fetcher, storage_context=storage_context,
            )
            paper.fulltext_status = status
            statuses[paper.id] = status
            rows_to_save.append((paper.id, status, evidence, pdf_path, candidate_url))
            label = "可获取" if status == FULLTEXT_STATUS_AVAILABLE else (
                "不可获取" if status == FULLTEXT_STATUS_UNAVAILABLE else "未验证")
            report(f"全文可获取性探测 {len(rows_to_save)}/{len(to_check)}："
                   f"{paper.title[:44]} → {label}")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        tasks = [asyncio.create_task(_run_one(p)) for p in to_check]
        try:
            try:
                done, pending = await asyncio.wait(
                    tasks, timeout=max(0.0, deadline - loop.time()))
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            for task in done:
                exc = task.exception()
                if exc is not None:
                    logger.debug("fulltext availability task failed: %s", exc)
            for task in pending:
                task.cancel()
                # Pending papers deliberately stay ``unknown``.
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            try:
                await fetcher.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("fulltext fetcher close failed: %s", e)
        if rows_to_save:
            db.set_fulltext_statuses(rows_to_save)
    finally:
        db.close()
    return statuses
