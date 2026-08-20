"""Reader Agent: PDF fetch + multimodal understanding + structured extraction.

The per-paper pipeline (the chokepoint is _process_single_paper):
  Stage 1   structure parse (Docling layout/OCR/table; PyMuPDF fallback) →
            sections with page numbers + figure/table/formula elements + raw_text
  Stage 1.5 scanned-page recovery — VLM-OCR the pages Docling couldn't (was a
            hard ``no_text`` failure before)
  Stage 2   VLM element understanding (budget-capped, partial-failure tolerant,
            image-hash + doc_fingerprint cached so re-reads cost zero VLM tokens)
  Stage 3   persist elements (global table + global vector collection) + attach
            light element refs to the summary
Extraction (LLM) runs on recovered text/sections; RAG indexing is session-scoped.

Cross-cutting:
- section-aware chunked full-text extraction (map-reduce) instead of text[:8000];
- concurrent per-paper processing (asyncio.gather + Semaphore);
- on-disk extraction cache (summary_cache table) so re-runs cost zero LLM tokens;
- explicit failures: JSON parse retry + empty-shell detection -> state["read_failures"].
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from langchain_core.messages import HumanMessage

from core.blocking import run_cpu_bound
from core.config import get_settings
from core.llm import get_llm
from core.paper_search_settings_store import paper_abstract_text
from core.models import FIELD_PROFILES, Paper, PaperSummary, Reference
from core.multimodal import recover_scanned_pages, understand_elements
from core.prompts.reader_prompts import build_extraction_prompt
from core.reading_policy import (
    FULLTEXT_STATUS_AVAILABLE,
    FULLTEXT_STATUS_UNAVAILABLE,
    FULLTEXT_STATUS_UNKNOWN,
    is_full_text,
    summary_has_full_text,
)
from core.state import ResearchState
from tools.pdf.fetcher import PDFFetcher
from tools.pdf.structure import get_structure_parser
from tools.pdf.structure.models import PaperElement, ParsedPaperDocument
from tools.storage.database import Database
from tools.storage.vectorstore import VectorStore

logger = logging.getLogger(__name__)

# Phase 2: a single shared VectorStore so the embedder model loads once.
_VECTORSTORE: VectorStore | None = None
_VECTORSTORES: dict[str, VectorStore] = {}


def _get_vectorstore(storage_context=None) -> VectorStore:
    """Return one shared VectorStore per channel-specific Chroma root."""
    global _VECTORSTORE
    if storage_context is None:
        if _VECTORSTORE is None:
            _VECTORSTORE = VectorStore()
        return _VECTORSTORE
    key = str(storage_context.chroma_dir)
    if key not in _VECTORSTORES:
        _VECTORSTORES[key] = VectorStore(storage_context=storage_context)
    return _VECTORSTORES[key]

# Per-paper LLM concurrency (same order of magnitude as core/llm.py's per-model
# semaphore 3) to avoid 429s while still overlapping I/O-bound extraction calls.
_EXTRACT_SEMAPHORE = 3

# Section ordering priority for chunk assembly (most informative first).
_SECTION_PRIORITY = [
    "abstract", "introduction", "background", "related",
    "method", "methodology", "approach",
    "experiment", "evaluation", "result",
    "discussion", "conclusion",
]

_WHITESPACE_RE = re.compile(r"\s+")


async def reader_agent(
    state: ResearchState,
    progress_callback=None,
    core_limit: int | None = None,
    read_mode: str = "abstract",
) -> ResearchState:
    """Process papers: download PDFs, parse, extract structured summaries.

    Returns state with ``paper_summaries`` (dict paper_id -> PaperSummary) and
    ``read_failures`` (list[{paper_id, title, reason}]).
    """
    papers: list[Paper] = state.get("papers", [])
    if not papers:
        state["current_phase"] = "read_done"
        state["read_failures"] = []
        return state

    def report(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    profile_key = state.get("field_profile", "general")
    active_fields = FIELD_PROFILES.get(profile_key, FIELD_PROFILES["general"])

    s = get_settings()
    storage_context = state.get("storage_context")
    if core_limit is None:
        core_limit = len(papers)
    papers_to_process = papers[:core_limit]

    report(f"Processing {len(papers_to_process)} papers (mode: {read_mode})")

    # --- Phase 1: Download PDFs (skip if abstract-only mode) ---
    read_mode = state.get("read_mode", read_mode)
    fetch_origin = state.get("fetch_origin", "automatic")
    from core.paper_search_settings_store import (
        capability_skip_result, disclose_fetch_policy, get_paper_search_policy,
        paper_capability_source, remote_download_allowed,
        source_capability_enabled,
    )
    fetch_policy = get_paper_search_policy()
    remote_fetch_blocked = (read_mode == "full" and
                            not remote_download_allowed(fetch_origin, fetch_policy))
    state["remote_fetch_blocked"] = remote_fetch_blocked
    n_with_pdf = 0
    if read_mode == "full":
        if remote_fetch_blocked and disclose_fetch_policy(fetch_policy):
            report("管理员当前限制远程论文全文拉取；将复用本地缓存，其他论文按摘要级处理。")
        fetcher = PDFFetcher(
            s.reader.pdf_dir, storage_context=storage_context,
            fetch_origin=fetch_origin,
        )
        pdf_paths = await fetcher.fetch_many(papers_to_process)
        await fetcher.close()

        for paper in papers_to_process:
            paper.pdf_path = pdf_paths.get(paper.id)

        n_with_pdf = len(pdf_paths)
        capability_skips: list[dict] = []
        for paper in papers_to_process:
            if getattr(paper, "pdf_path", None):
                continue
            allowed, reason = source_capability_enabled(
                paper_capability_source(paper, "fulltext"),
                "fulltext", fetch_policy,
            )
            if not allowed:
                skip = capability_skip_result(
                    paper_capability_source(paper, "fulltext"), "fulltext", reason
                )
                skip.update({"paper_id": paper.id, "title": paper.title or paper.id})
                capability_skips.append(skip)
        state["capability_skips"] = capability_skips
        if not remote_fetch_blocked:
            report(
                f"Downloaded/reused {n_with_pdf} PDFs; "
                f"{len(papers_to_process) - n_with_pdf} will stay abstract-level"
            )
        elif not disclose_fetch_policy(fetch_policy):
            report(f"Full-text evidence ready for {n_with_pdf}/{len(papers_to_process)} papers")
    else:
        state["capability_skips"] = []
        report(f"Abstract-only mode: skipping PDF download for {len(papers_to_process)} papers")

    # --- Phase 2+3: Parse + Extract (concurrent with progress) ---
    db = Database(storage_context=storage_context) if storage_context is not None else Database(s.storage.sqlite_path)
    semaphore = asyncio.Semaphore(_EXTRACT_SEMAPHORE)
    session_id = state.get("session_id", "")

    async def _run(i: int, paper: Paper):
        async with semaphore:
            return i, paper, await _process_single_paper(
                paper, active_fields, db, profile_key, read_mode, session_id,
                storage_context=storage_context,
            )

    raw_results = await asyncio.gather(*[_run(i, p) for i, p in enumerate(papers_to_process)])

    summaries: dict[str, PaperSummary] = {}
    failures: list[dict[str, str]] = []
    fulltext_fallbacks: list[dict[str, str]] = []
    capability_skip_ids = {
        row.get("paper_id") for row in state.get("capability_skips", [])
    }
    fulltext_status_rows: list[tuple[str, str, str, str, str]] = []
    for _i, paper, result in raw_results:
        summary, reason = result
        if summary is not None:
            summaries[paper.id] = summary
            if read_mode == "full" and not summary_has_full_text(summary):
                # Full attempt was requested but this paper ended at abstract
                # level. Report the real level so callers never label it full.
                fallback_reason = (
                    "source_capability_disabled" if paper.id in capability_skip_ids
                    else "remote_fetch_restricted" if remote_fetch_blocked and not getattr(paper, "pdf_path", None)
                    else "oa_fulltext_unavailable" if not getattr(paper, "pdf_path", None)
                    else "fulltext_parse_degraded"
                )
                fulltext_fallbacks.append({
                    "paper_id": paper.id,
                    "title": paper.title or paper.id,
                    "reason": fallback_reason,
                })
                fallback_status = (FULLTEXT_STATUS_UNKNOWN if fallback_reason == "remote_fetch_restricted"
                                   else FULLTEXT_STATUS_UNAVAILABLE)
                paper.fulltext_status = fallback_status
                fulltext_status_rows.append(
                    (paper.id, fallback_status, fallback_reason,
                     paper.pdf_path or "", paper.pdf_url or ""))
            elif read_mode == "full" and summary_has_full_text(summary):
                paper.fulltext_status = FULLTEXT_STATUS_AVAILABLE
                fulltext_status_rows.append(
                    (paper.id, FULLTEXT_STATUS_AVAILABLE, "deep_read_full_verified",
                     paper.pdf_path or "", paper.pdf_url or ""))
        else:
            failures.append({"paper_id": paper.id, "title": paper.title, "reason": reason or "unknown"})
            if read_mode == "full":
                paper.fulltext_status = FULLTEXT_STATUS_UNAVAILABLE
                fulltext_status_rows.append(
                    (paper.id, FULLTEXT_STATUS_UNAVAILABLE, f"read_failed:{reason or 'unknown'}",
                     paper.pdf_path or "", paper.pdf_url or ""))
            report(f"Failed: {paper.title[:50]}... ({reason})")

    n_full = sum(1 for s in summaries.values() if summary_has_full_text(s))
    report(
        f"Done: {len(summaries)} summaries"
        + (f" ({n_full} full-text)" if read_mode == "full" and summaries else "")
        + (f", {len(failures)} failed" if failures else "")
    )

    # --- Persist metadata into the SQLite cost-cache (NOT a user-facing
    # library: cross-session memory is intentionally not exposed). ---
    try:
        db.save_papers(papers_to_process)
        if fulltext_status_rows:
            db.set_fulltext_statuses(fulltext_status_rows)
    except Exception as e:
        logger.debug("paper metadata persist skipped: %s", e)
    db.close()

    state["paper_summaries"] = summaries
    state["read_failures"] = failures
    state["fulltext_fallbacks"] = fulltext_fallbacks
    state["current_phase"] = "read_done"
    return state


def _full_text_is_abstract(full_text: str | None, abstract: str | None) -> bool:
    """Detect old cache rows where an abstract was stored as ``full_text``.

    Older code set ``full_text = source_text`` whenever ``paper.pdf_path`` was
    truthy, even if PDF parsing failed and ``source_text`` was only the
    abstract. Those rows otherwise pass the length check and must be healed.
    """
    if not full_text or not abstract:
        return False
    return _normalize(full_text) == _normalize(abstract)


def _section_outline(doc: ParsedPaperDocument | None) -> list[dict]:
    if doc is None:
        return []
    return [
        {"title": sec.title.strip(), "page_start": int(sec.page_start or 0),
         "page_end": int(sec.page_end or sec.page_start or 0)}
        for sec in doc.sections if sec.title.strip()
    ]


def _document_info(
    paper: Paper, doc: ParsedPaperDocument | None, *, read_level: str,
    text_chars: int = 0, elements: list | None = None,
) -> dict:
    els = elements if elements is not None else (doc.elements if doc is not None else [])
    pdf_fetched = bool(paper.pdf_path)
    parse_status = (
        "no_pdf" if not pdf_fetched else
        "parse_failed" if doc is None else
        "parsed_full" if read_level == "full" else
        "parsed_insufficient"
    )
    return {
        "read_level": read_level,
        "pdf_fetched": pdf_fetched,
        "parse_status": parse_status,
        "parser_backend": doc.parser_backend if doc is not None else "",
        "page_count": int(doc.page_count or 0) if doc is not None else 0,
        "text_chars": int(text_chars or 0),
        "section_count": len(doc.sections) if doc is not None else 0,
        "is_scanned": bool(doc.is_scanned) if doc is not None else False,
        "ocr_status": doc.ocr_status if doc is not None else "not_attempted",
        "ocr_chars": int(doc.ocr_chars or 0) if doc is not None else 0,
        "element_count": len(els),
        "vision_understood_count": sum(1 for el in els if getattr(el, "understanding", None)),
    }


def _summary_has_document_metadata(summary: PaperSummary) -> bool:
    info = summary.document_info or {}
    if "section_count" not in info or "read_level" not in info:
        return False
    return int(info.get("section_count") or 0) == 0 or bool(summary.section_outline)


async def _process_single_paper(
    paper: Paper,
    active_fields: list[str],
    db: Database,
    profile_key: str,
    read_mode: str,
    session_id: str = "",
    storage_context=None,
) -> tuple[PaperSummary | None, str | None]:
    """Process one paper through the multimodal understanding pipeline.

    Stages:
      1.  Structure parse (Docling primary, PyMuPDF fallback) → sections with
          page numbers + element inventory (figures/tables/formulas) + raw_text.
      1.5 Scanned-page recovery: if the parse flagged ``is_scanned`` (Docling's
          OCR underperformed), VLM-OCR the first pages and fold the text in —
          this replaces the old hard ``no_text`` failure for scanned PDFs.
      2.  VLM element understanding (budget-capped, partial-failure tolerant),
          fingerprint-gated so a re-read with an unchanged PDF hydrates the
          stored understanding for free (zero VLM tokens).
      3.  Persist elements to the global table + global vector collection +
          attach light element refs to the summary.
    Extraction (LLM) runs on the recovered raw_text / sections exactly as
    before; indexing into the session-scoped RAG store is best-effort.

    Returns (summary, failure_reason). On success failure_reason is None; on
    failure summary is None and reason is "no_text" / "extraction_failed" /
    "empty_shell".
    """
    s = get_settings()
    from core.paper_search_settings_store import (
        paper_capability_source, source_capability_enabled,
    )
    abstract_allowed, _abstract_reason = source_capability_enabled(
        paper_capability_source(paper, "abstract"), "abstract"
    )

    # --- Cache lookup (Phase 0) ---
    # A ``full``-mode cache row is only valid when it contains real full text.
    # Older builds cached an abstract fallback under the full key, which made
    # later deep_read calls report "全文级" while only an abstract was stored.
    # Discard those rows and re-fetch; the same guard heals existing DB rows.
    cached = db.get_cached_summary(paper.id, profile_key, read_mode)
    if cached:
        try:
            cached_summary = _summary_from_dict(paper.id, json.loads(cached))
        except Exception:
            logger.warning("Corrupt cache for %s, re-extracting", paper.id)
            cached_summary = None
        if cached_summary is not None and read_mode == "full" and (
            not summary_has_full_text(cached_summary)
            or _full_text_is_abstract(cached_summary.full_text, paper_abstract_text(paper))
        ):
            logger.warning(
                "Discarding invalid full-mode cache for %s (full_text missing or "
                "just the abstract), refetching",
                paper.id,
            )
            try:
                db.delete_cached_summary(paper.id, profile_key, read_mode)
            except Exception as e:  # noqa: BLE001
                logger.debug("invalid full-mode cache delete skipped: %s", e)
            cached_summary = None
        if (cached_summary is not None and read_mode == "full"
                and paper.pdf_path and not _summary_has_document_metadata(cached_summary)):
            # Legacy full caches predate section_outline/document_info. Reparse
            # structure once; fingerprint-gated element hydration guarantees no
            # repeated VLM spend, then persist the healed lightweight metadata.
            assets_dir = str(storage_context.blob_dir / "element_assets") \
                if storage_context is not None and storage_context.channel == "openai_api" \
                else s.reader.assets_dir
            healed_doc = await parse_and_understand(
                paper, db, assets_dir=assets_dir, storage_context=storage_context,
                session_id=session_id,
            )
            if healed_doc is not None and is_full_text(healed_doc.raw_text):
                cached_summary.section_outline = _section_outline(healed_doc)
                cached_summary.document_info = _document_info(
                    paper, healed_doc, read_level="full",
                    text_chars=len(cached_summary.full_text or healed_doc.raw_text),
                )
                cached_summary.elements = [e.short_ref() for e in healed_doc.elements]
                try:
                    db.save_cached_summary(
                        paper.id, profile_key, "full",
                        json.dumps(cached_summary.to_dict()),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug("legacy full cache metadata heal write skipped: %s", e)
                await run_cpu_bound(
                    _index_paper, paper, cached_summary, healed_doc, "full",
                    session_id, storage_context=storage_context,
                )
        if (cached_summary is not None and not abstract_allowed
                and not summary_has_full_text(cached_summary)):
            cached_summary = None
        if cached_summary is not None:
            # A summary-cache hit must also restore the independently persisted
            # multimodal inventory.  Older cache rows predate ``summary.elements``
            # and the global vector collection may have been rebuilt, so hydrate
            # refs from SQLite and best-effort re-upsert the element index without
            # making any VLM call.
            try:
                stored_elements = db.get_elements(paper.id)
                if stored_elements:
                    cached_summary.elements = [_stored_element_ref(r) for r in stored_elements]
                    _index_elements_for_context(
                        paper, [_stored_element_model(r) for r in stored_elements], storage_context
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug("cached element restore skipped for %s: %s", paper.id, e)
            # Indexing is best-effort and must never invalidate a cache hit.
            await run_cpu_bound(
                _index_paper, paper, cached_summary, None, read_mode, session_id,
                storage_context=storage_context,
            )
            return cached_summary, None

    # --- Full attempt without an OA PDF: reuse the abstract cache if present.
    # This keeps repeated deep_read calls free for paywalled papers while the
    # full key stays empty, so a later OA copy can still be picked up.
    if read_mode == "full" and not paper.pdf_path and abstract_allowed:
        cached_abstract = db.get_cached_summary(paper.id, profile_key, "abstract")
        if cached_abstract:
            try:
                abstract_summary = _summary_from_dict(paper.id, json.loads(cached_abstract))
            except Exception:
                abstract_summary = None
            if abstract_summary is not None:
                if not abstract_summary.document_info:
                    abstract_summary.document_info = _document_info(
                        paper, None, read_level="abstract",
                        text_chars=len(paper_abstract_text(paper)),
                    )
                await run_cpu_bound(
                    _index_paper, paper, abstract_summary, None, "abstract",
                    session_id, storage_context=storage_context,
                )
                return abstract_summary, None

    # --- Stage 1 + 1.5 + 2 + 3: parse → recover → understand → persist ---
    # Shared with on-demand full-text escalation (tools_impl._ensure_fulltext).
    # Never raises; a None doc degrades to abstract-only below.
    doc = None
    if paper.pdf_path:
        assets_dir = str(storage_context.blob_dir / "element_assets") if storage_context is not None and storage_context.channel == "openai_api" else s.reader.assets_dir
        doc = await (
            parse_and_understand(
                paper, db, assets_dir=assets_dir, storage_context=storage_context,
                session_id=session_id,
            )
            if storage_context is not None
            else parse_and_understand(paper, db, assets_dir=assets_dir)
        )

    # ``doc_text`` is only real full text extracted from a downloaded PDF.
    # An abstract must never become summary.full_text, even when a PDF download
    # was attempted (parse failure / scanned with no recovery / no OA PDF).
    doc_text = ""
    if doc is not None and is_full_text(doc.raw_text):
        doc_text = doc.raw_text
    source_text = doc_text or paper_abstract_text(paper) or ""

    if not source_text:
        return None, "no_text"

    # --- Extraction ---
    # Full mode chunked extraction now runs whenever a real PDF body exists,
    # including PDFs without detected sections (raw-text chunk fallback).
    # A downloaded-but-unparseable PDF therefore never stores its abstract
    # under the full cache key.
    if read_mode == "full" and doc_text:
        data = await _extract_chunked(paper, doc, active_fields)
        full_text = doc_text  # retained for downstream RAG (Phase 2)
    else:
        data = await _extract_single(paper, source_text, active_fields)
        full_text = None

    if data is None:
        return None, "extraction_failed"
    if _is_empty_shell(data, active_fields):
        return None, "empty_shell"

    summary = _build_summary(paper, data, active_fields, full_text)
    summary.section_outline = _section_outline(doc) if full_text else []
    summary.document_info = _document_info(
        paper, doc, read_level="full" if full_text else "abstract",
        text_chars=len(full_text or source_text),
    )
    # Attach light element refs (element_id/kind/page/caption) so the summary
    # and frontend can address "Figure 3" without lugging the full VLM payload.
    if doc is not None and doc.elements:
        summary.elements = [e.short_ref() for e in doc.elements]

    # --- Cache write (Phase 0) ---
    # Cache under the level that was actually achieved. Full request + no OA
    # PDF/parse failure stores an abstract row, leaving the full key free for
    # future retries.
    effective_read_mode = "full" if full_text else "abstract"
    try:
        db.save_cached_summary(paper.id, profile_key, effective_read_mode,
                               json.dumps(summary.to_dict()))
    except Exception as e:
        logger.warning("Cache write failed for %s: %s", paper.id, e)

    # --- Index into the session-scoped RAG vector store (best-effort) ---
    await run_cpu_bound(
        _index_paper, paper, summary, doc if full_text else None,
        effective_read_mode, session_id, storage_context=storage_context,
    )

    return summary, None


def persist_api_element_assets(
    paper: Paper, doc: ParsedPaperDocument, storage_context, *, session_id: str = ""
) -> None:
    """Move parser crops into registered API artifacts and rewrite asset paths."""
    if storage_context is None or storage_context.channel != "openai_api":
        return
    from core.api_artifact_store import ApiArtifactStore
    from core.api_storage_store import ApiStorageStore
    artifact_store = ApiArtifactStore(ApiStorageStore(storage_context))
    for element in doc.elements:
        source = Path(element.asset_path) if element.asset_path else None
        if source is None or not source.is_file():
            continue
        if paper.source == "upload" and session_id:
            artifact = artifact_store.save_private_upload(
                source, session_id=session_id, logical_name=source.name,
                mime_type="image/png", category="element_asset",
            )
        else:
            artifact = artifact_store.save_file(
                source, category="element_asset", scope="public",
                logical_name=source.name, mime_type="image/png",
                ttl_seconds=artifact_store.storage.get_policy().cache_ttl_seconds,
            )
        if source.resolve() != artifact.path.resolve():
            source.unlink(missing_ok=True)
        element.asset_path = str(artifact.path)


async def parse_and_understand(
    paper: Paper, db: Database, *, assets_dir: str | None = None,
    storage_context=None, session_id: str = "",
) -> ParsedPaperDocument | None:
    """Stage 1 + 1.5 + 2 + 3 for one paper's PDF (shared entry point).

    Used by the read pipeline (_process_single_paper) and on-demand full-text
    escalation (tools_impl._ensure_fulltext) so both follow the exact same
    multimodal path: structure parse → scanned-page recovery → VLM element
    understanding → global persist + index.

    Returns the parsed document (text recovered, elements understood) or None
    when the PDF can't be parsed at all — callers handle None by degrading.
    Element understanding/persistence is fingerprint-gated, so re-reading a
    paper (or escalating it to full-text after a deep_read) costs zero VLM.
    Never raises.
    """
    if not paper.pdf_path:
        return None
    assets_dir = assets_dir or get_settings().reader.assets_dir
    try:
        doc = await run_cpu_bound(
            get_structure_parser().parse, paper.pdf_path,
            paper_id=paper.id, assets_dir=assets_dir,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Parse failed for %s: %s", paper.id, e)
        return None

    if storage_context is not None and storage_context.channel == "openai_api":
        await run_cpu_bound(
            persist_api_element_assets, paper, doc, storage_context,
            session_id=session_id,
        )
        for element in doc.elements:
            asset = Path(element.asset_path) if element.asset_path else None
            if asset is not None and asset.is_file():
                try:
                    storage_context.secure_private_file(asset)
                except Exception:  # noqa: BLE001
                    pass

    # Stage 1.5 — scanned-page recovery. The old pipeline hard-failed with
    # "no_text" here; we now VLM-OCR the first pages before giving up.
    if doc.is_scanned:
        doc.ocr_status = "attempted"
        try:
            recovered = await (
                recover_scanned_pages(paper.pdf_path, storage_context=storage_context)
                if storage_context is not None else recover_scanned_pages(paper.pdf_path)
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("scanned-page recovery failed for %s: %s", paper.id, e)
            recovered = ""
        if recovered:
            doc.ocr_status = "recovered"
            doc.ocr_chars = len(recovered.strip())
            doc.raw_text = (doc.raw_text + "\n\n" + recovered).strip() if doc.raw_text else recovered
        else:
            doc.ocr_status = "unavailable_or_empty"
            doc.ocr_chars = 0
    else:
        doc.ocr_status = "not_needed"
        doc.ocr_chars = 0

    # Stage 2 + 3 — element understanding + persist (fingerprint-gated).
    if doc.elements:
        await _understand_and_persist_elements(paper, doc, db, storage_context=storage_context)
    return doc


async def _understand_and_persist_elements(
    paper: Paper, doc: ParsedPaperDocument, db: Database, *, storage_context=None
) -> None:
    """Stage 2 (VLM understand) + Stage 3 (persist), fingerprint-gated.

    If the stored elements already match this PDF's doc_fingerprint, the VLM
    understanding is hydrated from the global table for free (no VLM call, no
    re-persist, no re-index). Otherwise the VLM analyzers run and the result is
    persisted + indexed. Best-effort: any failure is logged and swallowed so it
    never blocks the read pipeline.
    """
    try:
        stored_fp = db.elements_fingerprint(paper.id)
        if stored_fp and stored_fp == doc.doc_fingerprint:
            _hydrate_elements(doc, db.get_elements(paper.id))
            # Re-upsert is embedding-only and repairs a missing/rebuilt Chroma
            # collection while still guaranteeing zero VLM cost on cache hits.
            await run_cpu_bound(
                _index_elements_for_context, paper, doc.elements, storage_context
            )
            return  # current; nothing to re-understand or re-persist
        await (
            understand_elements(doc.elements, focus="", storage_context=storage_context)
            if storage_context is not None else understand_elements(doc.elements, focus="")
        )
        db.save_elements(paper.id, doc.elements, doc.doc_fingerprint)
        await run_cpu_bound(
            _index_elements_for_context, paper, doc.elements, storage_context
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("element understand/persist failed for %s: %s", paper.id, e)



def _stored_element_ref(row: dict) -> dict:
    return {
        "element_id": row.get("element_id", ""),
        "kind": row.get("kind", ""),
        "page": row.get("page", 0),
        "section": row.get("section", ""),
        "caption": row.get("caption", ""),
    }


def _stored_element_model(row: dict) -> PaperElement:
    bbox = row.get("bbox")
    return PaperElement(
        element_id=row.get("element_id", ""), kind=row.get("kind", ""),
        ordinal=int(row.get("ordinal") or 0), page=int(row.get("page") or 0),
        section=row.get("section", ""), caption=row.get("caption", ""),
        bbox=tuple(bbox) if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None,
        asset_path=row.get("asset_path"), image_hash=row.get("image_hash"),
        docling_extract=row.get("docling_extract") or {},
        understanding=row.get("understanding") or None,
    )

def _hydrate_elements(doc: ParsedPaperDocument, stored_rows: list) -> None:
    """Copy understanding + docling_extract from stored rows onto freshly parsed
    elements (matched by element_id), so a fingerprint hit costs zero VLM.

    The fresh parse already carries structural fields (kind/page/bbox/caption/
    asset_path/image_hash) and Docling's mechanical extract; we only restore the
    expensive VLM ``understanding`` and any Docling extract the stored row has.
    """
    by_id: dict[str, dict] = {}
    for row in stored_rows or []:
        eid = row.get("element_id") if isinstance(row, dict) else getattr(row, "element_id", "")
        if eid:
            by_id[eid] = row
    for el in doc.elements:
        row = by_id.get(el.element_id)
        if not row:
            continue
        und = row.get("understanding") if isinstance(row, dict) else getattr(row, "understanding", None)
        if und:
            el.understanding = und
        dl = row.get("docling_extract") if isinstance(row, dict) else getattr(row, "docling_extract", None)
        if dl:
            el.docling_extract = {**(el.docling_extract or {}), **dl}


def _index_elements_global(paper: Paper, elements: list, *, storage_context=None) -> None:
    """Index a paper's elements into the GLOBAL elements collection (no session).

    Best-effort: a failure is logged and swallowed — the SQLite row is already
    written, so retrieval can still fall back to kind/page filtering.
    """
    try:
        vs = _get_vectorstore(storage_context)
        vs.upsert_elements(paper, elements)
    except Exception as e:  # noqa: BLE001
        logger.debug("element vector index skipped for %s: %s", paper.id, e)



def _index_elements_for_context(paper: Paper, elements: list, storage_context=None) -> None:
    if storage_context is None:
        _index_elements_global(paper, elements)
    else:
        _index_elements_global(paper, elements, storage_context=storage_context)

def _index_paper(paper: Paper, summary: PaperSummary, parsed: ParsedPaperDocument | None,
                 read_mode: str, session_id: str = "", *, storage_context=None) -> None:
    """Index a paper's summary + full-text chunks into the session-scoped vector store.

    Best-effort: any failure (model unavailable, store closed) is logged and
    swallowed so it never blocks the read pipeline. Callers execute this
    synchronous local-ML/storage work through ``run_cpu_bound``.
    """
    try:
        vs = _get_vectorstore(storage_context)
        vs.upsert_paper_summary(paper, summary, session_id=session_id)
        if read_mode == "full":
            sections = parsed.sections if parsed else []
            full_text = getattr(summary, "full_text", None)
            vs.upsert_fulltext_chunks(
                paper, session_id=session_id, parsed_sections=sections,
                full_text=full_text,
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("vector index skipped for %s: %s", paper.id, e)


# Section-aware chunking (map-reduce)

def _section_rank(title: str) -> tuple[int, int]:
    low = title.lower()
    for rank, pat in enumerate(_SECTION_PRIORITY):
        if pat in low:
            return 0, rank
    return 1, 0


def _build_chunks(parsed: ParsedPaperDocument, max_chars: int) -> list[str]:
    """Assemble section-labelled chunks ordered by informativeness.

    Sections are ranked (abstract first, ...), then greedily packed into chunks
    up to ``max_chars`` each. Falls back to raw_text chunking if no sections.
    """
    sections = [sec for sec in parsed.sections if sec.text.strip()]
    sections.sort(key=lambda sec: _section_rank(sec.title))

    raw_text = parsed.raw_text or ""
    if sections:
        section_chars = sum(len(sec.text.strip()) for sec in sections)
        # Sections normally mirror the parsed body. When they don't (e.g. a
        # scanned PDF whose VLM-OCR recovery was appended only to raw_text),
        # falling back to raw-text chunks is what actually reads the paper.
        if (section_chars < len(raw_text.strip()) * 0.6
                and len(raw_text.strip()) - section_chars > 500):
            sections = []

    if not sections:
        return [raw_text[i:i + max_chars] for i in range(0, len(raw_text), max_chars)] or [""]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for sec in sections:
        block = f"[{sec.title.strip()}]\n{sec.text.strip()}"
        if current and current_len + len(block) > max_chars:
            chunks.append("\n\n".join(current))
            current = [block]
            current_len = len(block)
        else:
            current.append(block)
            current_len += len(block) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [""]


async def _extract_chunked(
    paper: Paper, parsed: ParsedPaperDocument, active_fields: list[str]
) -> dict | None:
    """Map-reduce extraction: one LLM call per chunk, then rule-based merge."""
    s = get_settings()
    chunks = _build_chunks(parsed, s.reader.read_chunk_chars)
    if len(chunks) == 1:
        return await _extract_one(paper, chunks[0], active_fields, None, None)

    parts: list[dict] = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        partial = await _extract_one(paper, chunk, active_fields, idx + 1, total)
        if partial is not None:
            parts.append(partial)
    if not parts:
        return None
    return _merge_extractions(parts, active_fields) if len(parts) > 1 else parts[0]


async def _extract_single(
    paper: Paper, text: str, active_fields: list[str]
) -> dict | None:
    """Single-shot extraction (abstract mode or small full text)."""
    return await _extract_one(paper, text, active_fields, None, None)


async def _extract_one(
    paper: Paper,
    text: str,
    active_fields: list[str],
    part_idx: int | None,
    total_parts: int | None,
) -> dict | None:
    """One LLM extraction call with one JSON-retry on parse failure."""
    data = await _call_and_parse(text, active_fields, part_idx, total_parts, strict=False)
    if data is None:
        data = await _call_and_parse(text, active_fields, part_idx, total_parts, strict=True)
    return data


async def _call_and_parse(
    text: str,
    active_fields: list[str],
    part_idx: int | None,
    total_parts: int | None,
    strict: bool,
) -> dict | None:
    """Invoke the light LLM and parse the JSON response (None on any failure)."""
    llm = get_llm("light")
    prompt = build_extraction_prompt(
        text, active_fields, part_idx=part_idx, total_parts=total_parts, strict=strict
    )
    try:
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        return _parse_json(resp.content.strip())
    except Exception as e:
        logger.error("Extraction call failed: %s", e)
        return None


def _parse_json(raw: str) -> dict | None:
    """Strip code fences and parse JSON; return None on failure."""
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _merge_extractions(parts: list[dict], active_fields: list[str]) -> dict:
    """Rule-based reduce: union list fields, concatenate distinct strings."""
    merged: dict = {}
    for f in active_fields:
        vals = [p.get(f) for p in parts if p.get(f)]
        if not vals:
            continue
        if isinstance(vals[0], list):
            merged[f] = _dedup_list_items(vals)
        else:
            merged[f] = _dedup_strings([str(v) for v in vals])
    refs = _dedup_list_items([p.get("references_raw", []) for p in parts if p.get("references_raw")])
    if refs:
        merged["references_raw"] = refs
    return merged


def _dedup_list_items(list_of_lists: list[list]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lst in list_of_lists:
        for item in lst:
            key = _normalize(item)
            if key and key not in seen:
                seen.add(key)
                out.append(str(item))
    return out


def _dedup_strings(strings: list[str]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for s in strings:
        key = _normalize(s)
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    return " ".join(out)


def _normalize(text: object) -> str:
    return _WHITESPACE_RE.sub(" ", str(text)).strip().lower()


def _is_empty_shell(data: dict, active_fields: list[str]) -> bool:
    """A valid-JSON but all-empty extraction is treated as a failure."""
    def _nonempty(v) -> bool:
        if isinstance(v, (list, tuple, dict)):
            return len(v) > 0
        return bool(str(v).strip())

    has_field = any(_nonempty(data.get(f)) for f in active_fields)
    has_refs = _nonempty(data.get("references_raw"))
    return not (has_field or has_refs)


def _build_summary(
    paper: Paper, data: dict, active_fields: list[str], full_text: str | None
) -> PaperSummary:
    summary = PaperSummary(paper_id=paper.id)
    for f in active_fields:
        value = data.get(f)
        if value is None:
            continue
        if hasattr(summary, f):
            setattr(summary, f, value)
    refs_raw = data.get("references_raw", [])
    if refs_raw:
        summary.references = [Reference(raw_text=r) for r in refs_raw[:50]]
    summary.full_text = full_text
    return summary


def _summary_from_dict(paper_id: str, data: dict) -> PaperSummary:
    """Reconstruct a PaperSummary from a cached dict (mirrors to_dict)."""
    summary = PaperSummary(paper_id=data.get("paper_id", paper_id))
    for f in [
        "research_problem", "methodology", "theoretical_framework", "sample_size",
        "key_findings", "contributions", "limitations", "datasets", "baselines",
        "interventions", "outcomes", "future_work",
    ]:
        val = data.get(f)
        if val not in (None, "", []):
            setattr(summary, f, val)
    refs_raw = data.get("references_raw", [])
    if refs_raw:
        summary.references = [Reference(raw_text=r) for r in refs_raw[:50]]
    if data.get("full_text"):
        summary.full_text = data["full_text"]
    if isinstance(data.get("elements"), list):
        summary.elements = data["elements"]
    if isinstance(data.get("section_outline"), list):
        summary.section_outline = data["section_outline"]
    if isinstance(data.get("document_info"), dict):
        summary.document_info = data["document_info"]
    return summary
