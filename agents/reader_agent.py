"""Reader Agent: uploaded-document multimodal understanding and extraction.

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
from core.reading_policy import is_full_text
from core.state import ResearchState
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
    """Process local uploads or produce abstract-level network summaries.

    Network papers deliberately never enter a remote PDF path.  A ``full``
    request for such a paper is downgraded to an explicit abstract summary and
    reports that the original must be uploaded for document-level analysis.
    Local upload documents may still use the structure/VLM/chunk pipeline.
    """
    papers: list[Paper] = list(state.get("papers") or [])
    if not papers:
        state["current_phase"] = "read_done"
        state["read_failures"] = []
        return state

    def report(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    profile_key = state.get("field_profile", "general")
    active_fields = FIELD_PROFILES.get(profile_key, FIELD_PROFILES["general"])
    storage_context = state.get("storage_context")
    if core_limit is None:
        core_limit = len(papers)
    papers_to_process = papers[:core_limit]
    requested_mode = state.get("read_mode", read_mode)
    report(f"处理 {len(papers_to_process)} 篇材料（网络论文仅摘要；上传文件可全文）")

    db = Database(storage_context=storage_context) if storage_context is not None else Database(get_settings().storage.sqlite_path)
    semaphore = asyncio.Semaphore(_EXTRACT_SEMAPHORE)
    session_id = state.get("session_id", "")

    async def run_one(index: int, paper: Paper):
        async with semaphore:
            effective = requested_mode if paper.source == "upload" else "abstract"
            return index, paper, await _process_single_paper(
                paper, active_fields, db, profile_key, effective, session_id,
                storage_context=storage_context,
            )

    raw = await asyncio.gather(*(run_one(i, p) for i, p in enumerate(papers_to_process)))
    summaries: dict[str, PaperSummary] = {}
    failures: list[dict[str, str]] = []
    for _index, paper, (summary, reason) in raw:
        if summary is not None:
            summaries[paper.id] = summary
        else:
            failures.append({"paper_id": paper.id, "title": paper.title, "reason": reason or "unknown"})
            report(f"处理失败：{paper.title[:50]}…（{reason or 'unknown'}）")
    try:
        db.save_papers(papers_to_process)
    except Exception as exc:  # noqa: BLE001
        logger.debug("paper metadata persist skipped: %s", exc)
    db.close()
    state["paper_summaries"] = summaries
    state["read_failures"] = failures
    state["capability_skips"] = []
    state["current_phase"] = "read_done"
    report(f"处理完成：{len(summaries)} 条摘要/上传全文材料" + (f"，{len(failures)} 条失败" if failures else ""))
    return state


def _full_text_is_abstract(full_text: str | None, abstract: str | None) -> bool:
    """Detect old cache rows where an abstract was stored as ``full_text``.

    Older code set ``full_text = source_text`` whenever a path was truthy, even
    if parsing failed and ``source_text`` was only the abstract. Those rows
    otherwise pass the length check and must be healed.
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
    document_ready = bool(paper.pdf_path)
    parse_status = (
        "missing_upload" if not document_ready else
        "parse_failed" if doc is None else
        "parsed_full" if read_level == "full" else
        "parsed_insufficient"
    )
    return {
        "read_level": read_level,
        "document_ready": document_ready,
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
    paper: Paper, active_fields: list[str], db: Database, profile_key: str,
    read_mode: str, session_id: str = "", storage_context=None,
) -> tuple[PaperSummary | None, str | None]:
    """Extract one abstract or one user-uploaded local document.

    ``paper.source != 'upload'`` is a hard boundary: no URL, cache status, or
    historical ``full_text`` row can promote it above abstract evidence.
    """
    settings = get_settings()
    is_upload = paper.source == "upload" or str(paper.id).startswith("upload:")
    effective_mode = "full" if is_upload and read_mode == "full" and paper.pdf_path else "abstract"
    cached = db.get_cached_summary(paper.id, profile_key, effective_mode)
    if cached:
        try:
            summary = _summary_from_dict(paper.id, json.loads(cached))
            if effective_mode != "full":
                summary.full_text = None
                summary.elements = []
            else:
                # Summary rows and element rows are independent caches.  A
                # private upload summary hit must not reparse or call the VLM,
                # but it should restore lightweight refs and repair the global
                # element vector index from the persisted upload-only rows.
                from tools.pdf.structure.models import PaperElement

                restored_elements = [PaperElement(
                    element_id=str(row.get("element_id") or ""),
                    kind=str(row.get("kind") or ""),
                    ordinal=int(row.get("ordinal") or 0),
                    page=int(row.get("page") or 0),
                    section=str(row.get("section") or ""),
                    caption=str(row.get("caption") or ""),
                    bbox=tuple(row["bbox"]) if row.get("bbox") else None,
                    asset_path=row.get("asset_path"),
                    image_hash=row.get("image_hash"),
                    docling_extract=row.get("docling_extract") or {},
                    understanding=row.get("understanding"),
                ) for row in db.get_elements(paper.id)]
                if restored_elements:
                    summary.elements = [element.short_ref() for element in restored_elements]
                    await run_cpu_bound(
                        _index_elements_for_context, paper, restored_elements,
                        storage_context,
                    )
            return summary, None
        except (TypeError, ValueError, json.JSONDecodeError):
            db.delete_cached_summary(paper.id, profile_key, effective_mode)

    doc = None
    if effective_mode == "full":
        assets_dir = (str(storage_context.blob_dir / "element_assets")
                      if storage_context is not None and storage_context.channel == "openai_api"
                      else settings.reader.assets_dir)
        doc = await (
            parse_and_understand(paper, db, assets_dir=assets_dir, storage_context=storage_context, session_id=session_id)
            if storage_context is not None else parse_and_understand(paper, db, assets_dir=assets_dir)
        )
    doc_text = doc.raw_text if doc is not None and is_full_text(doc.raw_text) else ""
    source_text = doc_text if effective_mode == "full" else (paper_abstract_text(paper) or paper.abstract or "")
    if not source_text.strip():
        return None, "no_text"
    data = await (_extract_chunked(paper, doc, active_fields) if effective_mode == "full" and doc_text
                  else _extract_single(paper, source_text, active_fields))
    if data is None:
        return None, "extraction_failed"
    if _is_empty_shell(data, active_fields):
        return None, "empty_shell"
    full_text = doc_text if effective_mode == "full" else None
    summary = _build_summary(paper, data, active_fields, full_text)
    summary.section_outline = _section_outline(doc) if full_text else []
    summary.document_info = _document_info(paper, doc, read_level="full" if full_text else "abstract",
                                           text_chars=len(full_text or source_text))
    if doc is not None and doc.elements:
        summary.elements = [element.short_ref() for element in doc.elements]
    try:
        db.save_cached_summary(paper.id, profile_key, "full" if full_text else "abstract",
                               json.dumps(summary.to_dict(), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cache write failed for %s: %s", paper.id, exc)
    await run_cpu_bound(_index_paper, paper, summary, doc if full_text else None,
                        "full" if full_text else "abstract", session_id,
                        storage_context=storage_context)
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
        if paper.source != "upload" or not session_id:
            raise ValueError("element artifacts require a session-private user upload")
        artifact = artifact_store.save_private_upload(
            source, session_id=session_id, logical_name=source.name,
            mime_type="image/png", category="element_asset",
        )
        if source.resolve() != artifact.path.resolve():
            source.unlink(missing_ok=True)
        element.asset_path = str(artifact.path)


async def parse_and_understand(
    paper: Paper, db: Database, *, assets_dir: str | None = None,
    storage_context=None, session_id: str = "",
) -> ParsedPaperDocument | None:
    """Parse a *user-uploaded* PDF through structure/OCR/VLM stages.

    Network-paper identifiers are rejected at this chokepoint.  This keeps
    every caller upload-only even if an old checkpoint or extension passes a
    legacy network ``Paper`` object.

    Returns the parsed document (text recovered, elements understood) or None
    when the PDF can't be parsed at all — callers handle None by degrading.
    Element understanding/persistence is fingerprint-gated, so revisiting an
    unchanged upload costs zero VLM calls.
    Never raises.
    """
    if paper.source != "upload" and not str(paper.id).startswith("upload:"):
        return None
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
    """Index a paper's summary + upload full-text chunks into the session-scoped vector store.

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
