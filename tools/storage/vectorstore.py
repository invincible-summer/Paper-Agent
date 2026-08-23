"""Session-scoped RAG vector store (ChromaDB, local embeddings).

Two cosine collections over a local PersistentClient:
  - summaries: one doc per (session, paper) — title + abstract (+ findings)
  - fulltext_chunks: per-section chunks of user-uploaded documents only

Every record carries a `session_id` metadata field and every read is filtered
by it: one conversation can never retrieve another conversation's papers.
There is intentionally NO cross-session memory.

All embeddings come from the local model in core/embeddings.py; if the model
is unavailable every method degrades to a no-op so the main pipeline is
unaffected. SQLite remains the source of truth for metadata; Chroma is an index.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.config import get_settings
from core.storage_context import StorageContext
from core.embeddings import embed_texts, get_embedder
from core.paper_search_settings_store import paper_abstract_text
from tools.retrieval.chunking import smart_chunks

logger = logging.getLogger(__name__)

_CHILD_SIZE = 500    # index unit: small child chunks (high retrieval precision)
_PARENT_SIZE = 1500  # context unit: parent passage returned to the LLM


@dataclass
class SearchHit:
    """A single retrieval result from a vector search."""
    id: str
    document: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


def build_fulltext_chunks(parsed_sections=None, full_text: str | None = None) -> list[dict]:
    """Build parent/child chunk records from parsed sections (or raw text).

    Each record is a small child chunk (~500 chars, sentence-boundary aware)
    carrying its parent passage (~1500 chars) so retrieval can do
    small-to-big: match on the child, hand the parent to the LLM.
    Record fields: section / text (child) / parent_text / parent_id.
    """
    out: list[dict] = []
    seen_text: set[str] = set()

    def _emit(section: str, parent_id: str, parent_text: str, body: str) -> None:
        for piece in smart_chunks(body, size=_CHILD_SIZE):
            key = piece.strip().lower()
            if not key or key in seen_text:
                continue
            seen_text.add(key)
            out.append({"section": section, "text": piece,
                        "parent_text": parent_text, "parent_id": parent_id})

    for si, sec in enumerate(parsed_sections or []):
        title = getattr(sec, "title", "") or ""
        body = (getattr(sec, "text", "") or "").strip()
        if not body:
            continue
        for pi, parent in enumerate(smart_chunks(body, size=_PARENT_SIZE)):
            _emit(title, f"s{si}::p{pi}", parent, parent)
    if not out and full_text:
        for pi, parent in enumerate(smart_chunks(full_text, size=_PARENT_SIZE)):
            _emit("full", f"full::p{pi}", parent, parent)
    return out


def _summary_document(summary, paper) -> str:
    """Build a level-1 document without crossing the evidence boundary.

    Network papers are indexed only from their *currently enabled, non-empty*
    abstract.  A restored ``PaperSummary`` may have been derived from the old
    remote-PDF path, so none of its fields are eligible for network indexing.
    Uploaded papers may still add their private full-document summary fields.
    """
    is_upload = getattr(paper, "source", "") == "upload" or str(
        getattr(paper, "id", "") or ""
    ).startswith("upload:")
    abstract = paper_abstract_text(paper).strip()
    if not is_upload and not abstract:
        return ""
    parts = [paper.title or "", abstract]
    if is_upload and summary is not None:
        findings = getattr(summary, "key_findings", None) or []
        if isinstance(findings, list) and findings:
            parts.append(" ".join(str(f) for f in findings[:5]))
        problem = getattr(summary, "research_problem", "") or ""
        if problem:
            parts.append(str(problem))
        methodology = getattr(summary, "methodology", "") or ""
        if methodology:
            parts.append(str(methodology))
    return "\n".join(p for p in parts if p).strip()


def _hits(res: dict) -> list[SearchHit]:
    """Flatten a Chroma query result (single query embedding) into SearchHits."""
    ids_batch = res.get("ids") or []
    docs_batch = res.get("documents") or []
    metas_batch = res.get("metadatas") or []
    dist_batch = res.get("distances") or []
    if not ids_batch:
        return []
    ids = ids_batch[0] if isinstance(ids_batch[0], list) else ids_batch
    docs = docs_batch[0] if docs_batch and isinstance(docs_batch[0], list) else (docs_batch or [])
    metas = metas_batch[0] if metas_batch and isinstance(metas_batch[0], list) else (metas_batch or [])
    dists = dist_batch[0] if dist_batch and isinstance(dist_batch[0], list) else (dist_batch or [])
    out: list[SearchHit] = []
    for i, _id in enumerate(ids):
        doc = docs[i] if i < len(docs) else ""
        meta = metas[i] if i < len(metas) else {}
        dist = dists[i] if i < len(dists) else 0.0
        out.append(SearchHit(id=str(_id), document=doc, metadata=meta or {}, score=round(1.0 - dist, 4)))
    return out


def _where(session_id: str | None, paper_id: str | None = None) -> dict | None:
    """Build a Chroma where filter combining session scope + optional paper."""
    clauses = []
    if session_id:
        clauses.append({"session_id": session_id})
    if paper_id:
        clauses.append({"paper_id": paper_id})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _elements_where(paper_ids: list[str], kind: str | None = None) -> dict:
    """Chroma where filter for the GLOBAL elements collection.

    Scoped by the caller-supplied paper set (preserves session isolation without
    storing a session_id on elements), optionally narrowed to one element kind.
    """
    clauses: list[dict] = [{"paper_id": {"$in": list(paper_ids)}}]
    if kind:
        clauses.append({"kind": kind})
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _element_document(element) -> str:
    """The text embedded for an element — the richest description available.

    Before VLM understanding (Stage 2) this is caption + Docling's mechanical
    extract (table markdown / formula LaTeX); once understanding is filled it
    also carries the VLM's description / components / summary / meaning.
    """
    parts: list[str] = []
    cap = getattr(element, "caption", "") or ""
    if cap:
        parts.append(f"[Caption] {cap}")
    und = getattr(element, "understanding", None) or {}
    if isinstance(und, dict):
        for key in ("description", "summary", "meaning", "role", "role_in_paper"):
            v = und.get(key)
            if v:
                parts.append(f"[{key}] {v}")
        for lst_key in ("components", "relations", "key_metrics", "comparison_axes"):
            v = und.get(lst_key)
            if isinstance(v, list) and v:
                parts.append(f"[{lst_key}] " + "; ".join(str(x) for x in v))
    dl = getattr(element, "docling_extract", None) or {}
    if isinstance(dl, dict):
        md = dl.get("markdown")
        if md:
            parts.append(f"[Table] {str(md)[:800]}")
        latex = dl.get("latex")
        if latex:
            parts.append(f"[Formula] {latex}")
    return "\n".join(parts).strip()


class VectorStore:
    """Thin wrapper over a ChromaDB PersistentClient with two collections.

    Every method returns None / [] / 0 / False on embedding or store failure
    so callers treat RAG as best-effort. available() lets a caller decide
    whether to attempt retrieval at all.
    """

    def __init__(self, path: str | None = None,
                 storage_context: StorageContext | None = None):
        if storage_context is not None:
            path = str(storage_context.chroma_dir)
            if storage_context.channel == "openai_api":
                storage_context.ensure_layout()
        self._path = path or get_settings().storage.chroma_dir
        self._client = None
        self._summaries = None
        self._chunks = None
        self._elements = None
        self._available: bool | None = None

    def _ensure(self) -> bool:
        """Lazily open the client + collections. Returns False on failure."""
        if self._available is not None:
            return self._available
        try:
            import chromadb
            from pathlib import Path

            Path(self._path).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self._path)
            self._summaries = self._client.get_or_create_collection(
                "summaries", metadata={"hnsw:space": "cosine"})
            self._chunks = self._client.get_or_create_collection(
                "fulltext_chunks", metadata={"hnsw:space": "cosine"})
            # Elements collection is GLOBAL (no session_id): VLM understanding is
            # expensive, so it is reused across conversations. Retrieval scopes
            # by the caller-supplied paper_id set, preserving session isolation.
            self._elements = self._client.get_or_create_collection(
                "elements", metadata={"hnsw:space": "cosine"})
            self._available = True
        except Exception as e:  # noqa: BLE001
            logger.warning("VectorStore unavailable: %s", e)
            self._available = False
        return self._available

    def available(self) -> bool:
        """True iff the embedder AND the store can both open."""
        if get_embedder() is None:
            return False
        return self._ensure()

    # --- Level 1: summaries (one doc per session+paper) ---

    def upsert_paper_summary(self, paper, summary=None, session_id: str = "") -> bool:
        """Index one paper (title+abstract, +findings when deep-read). Idempotent."""
        if not self._ensure():
            return False
        doc = _summary_document(summary, paper)
        if not doc:
            return False
        vecs = embed_texts([doc])
        if not vecs:
            return False
        try:
            self._summaries.upsert(
                ids=[f"{session_id}::{paper.id}"],
                embeddings=vecs,
                documents=[doc],
                metadatas=[{
                    "session_id": session_id,
                    "paper_id": paper.id,
                    "title": (paper.title or "")[:200],
                    "year": paper.year or 0,
                }],
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("summary upsert failed for %s: %s", paper.id, e)
            return False

    # --- Level 2: fulltext chunks ---

    def upsert_fulltext_chunks(self, paper, session_id: str = "",
                               parsed_sections=None, full_text: str | None = None) -> int:
        """Index per-section chunks of a user-uploaded document. Returns n chunks.

        Idempotent: clears prior chunks for the (session, paper) first.
        """
        if not self._ensure():
            return 0
        records = build_fulltext_chunks(parsed_sections, full_text)
        if not records:
            return 0
        docs = [r["text"] for r in records]
        vecs = embed_texts(docs)
        if not vecs or len(vecs) != len(records):
            return 0
        base = f"{session_id}::{paper.id}"
        ids = [f"{base}::c{i}" for i in range(len(records))]
        metas = [{
            "session_id": session_id,
            "paper_id": paper.id,
            "title": (paper.title or "")[:200],
            "section": (r["section"] or "")[:120],
            "chunk_idx": i,
            "year": paper.year or 0,
            "parent_id": r["parent_id"],
            "parent_text": r["parent_text"][:_PARENT_SIZE],
        } for i, r in enumerate(records)]
        try:
            self._delete_where(self._chunks, _where(session_id, paper.id))
            self._chunks.add(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)
            return len(records)
        except Exception as e:  # noqa: BLE001
            logger.warning("chunk upsert failed for %s: %s", paper.id, e)
            return 0

    def upsert_text_chunks(self, doc_id: str, title: str, text: str,
                           session_id: str = "", section: str = "upload") -> int:
        """Index raw text (e.g. an uploaded file) as chunks under doc_id."""
        if not self._ensure():
            return 0
        records = build_fulltext_chunks(None, text)
        if not records:
            return 0
        docs = [r["text"] for r in records]
        vecs = embed_texts(docs)
        if not vecs or len(vecs) != len(records):
            return 0
        base = f"{session_id}::{doc_id}"
        ids = [f"{base}::c{i}" for i in range(len(records))]
        metas = [{
            "session_id": session_id,
            "paper_id": doc_id,
            "title": (title or "")[:200],
            "section": section,
            "chunk_idx": i,
            "year": 0,
            "parent_id": r["parent_id"],
            "parent_text": r["parent_text"][:_PARENT_SIZE],
        } for i, r in enumerate(records)]
        try:
            self._delete_where(self._chunks, _where(session_id, doc_id))
            self._chunks.add(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)
            return len(records)
        except Exception as e:  # noqa: BLE001
            logger.warning("text chunk upsert failed for %s: %s", doc_id, e)
            return 0

    def delete_session(self, session_id: str) -> None:
        """Remove every vector belonging to a session (history deletion)."""
        if not session_id or not self._ensure():
            return
        for coll in (self._summaries, self._chunks):
            self._delete_where(coll, {"session_id": session_id})

    def _delete_where(self, collection, where: dict | None) -> None:
        if not where:
            return
        try:
            got = collection.get(where=where, include=[])
            ids = []
            raw_ids = got.get("ids") or []
            for batch in raw_ids:
                if isinstance(batch, list):
                    ids.extend(batch)
                elif isinstance(batch, str):
                    ids.append(batch)
            if ids:
                collection.delete(ids=ids)
        except Exception:  # noqa: BLE001
            pass

    # --- retrieval (always session-scoped) ---

    def search_summaries(self, query: str, session_id: str | None = None,
                         top_k: int = 20) -> list[SearchHit]:
        """Level-1 retrieval: most similar paper summaries in this session."""
        if not self._ensure():
            return []
        qv = embed_texts([query])
        if not qv:
            return []
        try:
            res = self._summaries.query(
                query_embeddings=qv, n_results=min(max(top_k, 1), 50),
                where=_where(session_id),
                include=["metadatas", "documents", "distances"])
            return _hits(res)
        except Exception as e:  # noqa: BLE001
            logger.warning("summary search failed: %s", e)
            return []

    def search_chunks(self, query: str, session_id: str | None = None,
                      paper_id: str | None = None, top_k: int = 5) -> list[SearchHit]:
        """Level-2 retrieval: uploaded-document chunks in this session."""
        if not self._ensure():
            return []
        qv = embed_texts([query])
        if not qv:
            return []
        try:
            res = self._chunks.query(
                query_embeddings=qv, n_results=min(max(top_k, 1), 30),
                where=_where(session_id, paper_id),
                include=["metadatas", "documents", "distances"])
            return _hits(res)
        except Exception as e:  # noqa: BLE001
            logger.warning("chunk search failed: %s", e)
            return []

    # --- Level 3: elements (figures / tables / formulas) — GLOBAL ---

    def upsert_elements(self, paper, elements: list) -> int:
        """Index a paper's elements (caption + VLM understanding + Docling extract).

        Idempotent: clears prior elements for the paper first. GLOBAL — no
        session_id — so VLM understanding is reused across conversations.
        Returns the number of elements indexed.
        """
        if not self._ensure():
            return 0
        records = [(el, _element_document(el)) for el in elements]
        records = [(el, doc) for el, doc in records if doc]
        if not records:
            return 0
        docs = [d for _, d in records]
        vecs = embed_texts(docs)
        if not vecs or len(vecs) != len(records):
            return 0
        ids = [el.element_id for el, _ in records]
        metas = [{
            "paper_id": paper.id,
            "kind": el.kind,
            "element_id": el.element_id,
            "page": int(el.page or 0),
            "section": (el.section or "")[:120],
            "title": (paper.title or "")[:200],
        } for el, _ in records]
        try:
            self._delete_where(self._elements, {"paper_id": paper.id})
            self._elements.add(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)
            return len(records)
        except Exception as e:  # noqa: BLE001
            logger.warning("element upsert failed for %s: %s", paper.id, e)
            return 0

    def search_elements(
        self,
        query: str,
        paper_ids: list[str],
        kind: str | None = None,
        top_k: int = 5,
    ) -> list[SearchHit]:
        """Retrieve elements semantically, scoped to the caller's paper set.

        The paper_id scope (not a session_id) is what preserves session
        isolation over this global collection.
        """
        if not self._ensure() or not paper_ids:
            return []
        qv = embed_texts([query])
        if not qv:
            return []
        try:
            res = self._elements.query(
                query_embeddings=qv,
                n_results=min(max(top_k, 1), 30),
                where=_elements_where(paper_ids, kind),
                include=["metadatas", "documents", "distances"],
            )
            return _hits(res)
        except Exception as e:  # noqa: BLE001
            logger.warning("element search failed: %s", e)
            return []

    def count_papers(self, session_id: str | None = None) -> int:
        """Number of indexed paper summaries (best-effort)."""
        if not self._ensure():
            return 0
        try:
            if not session_id:
                return self._summaries.count()
            got = self._summaries.get(where={"session_id": session_id}, include=[])
            return len(got.get("ids") or [])
        except Exception:  # noqa: BLE001
            return 0
