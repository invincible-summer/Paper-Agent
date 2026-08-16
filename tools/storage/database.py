"""SQLite storage for paper metadata (DESIGN D-009)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.models import Paper
from core.storage_context import StorageContext


_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT,
    year INTEGER,
    venue TEXT,
    doi TEXT,
    source TEXT,
    language TEXT,
    citation_count INTEGER,
    abstract TEXT,
    pdf_url TEXT,
    pdf_path TEXT,
    keywords TEXT,
    urls TEXT
);

-- Verified OA full-text availability (search-time PDF probe + deep_read
-- outcome cache). A URL-looking pdf_url is explicitly NOT stored as
-- ``available``; only a live-PDF probe, a local PDF, or a deep_read full
-- summary may write ``available`` here.
CREATE TABLE IF NOT EXISTS fulltext_status (
    paper_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,             -- available | unavailable | unknown
    evidence TEXT,                    -- oa_download_verified / parse_degraded / no_oa_url ...
    pdf_path TEXT,
    candidate_url TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id TEXT,
    stage TEXT,
    error_type TEXT,
    error_message TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    resolved BOOLEAN DEFAULT 0
);

-- Phase 0: extraction cache so re-runs don't re-spend LLM tokens.
CREATE TABLE IF NOT EXISTS summary_cache (
    paper_id TEXT NOT NULL,
    field_profile TEXT NOT NULL,
    read_mode TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (paper_id, field_profile, read_mode)
);

-- Multimodal (vision) result cache keyed by image hash + task + prompt version.
-- Vision calls are the most expensive operation in the pipeline; this makes a
-- second read of the same paper cost zero VLM tokens. prompt_version lets a
-- prompt bump invalidate stale entries without re-running the whole pipeline.
CREATE TABLE IF NOT EXISTS vision_cache (
    image_hash TEXT NOT NULL,
    task TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (image_hash, task, prompt_version)
);

-- Paper elements (figures / tables / formulas) — the addressable multimodal
-- knowledge layer. GLOBAL (no session_id): VLM understanding is expensive, so
-- it is reused across conversations. doc_fingerprint gates staleness — a
-- changed PDF yields a new fingerprint and elements are re-extracted.
CREATE TABLE IF NOT EXISTS paper_elements (
    element_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL,
    kind TEXT NOT NULL,            -- figure | table | formula
    ordinal INTEGER,
    page INTEGER,
    section TEXT,
    caption TEXT,
    bbox TEXT,                      -- JSON [x0,y0,x1,y1] in PDF points, or NULL
    asset_path TEXT,                -- data/assets/<paper_id>/<kind>_<n>.png
    image_hash TEXT,                -- sha256(crop bytes); vision cache key
    docling_extract TEXT,           -- JSON: {markdown} for tables, {latex} for formulas
    understanding TEXT,             -- JSON: VLM semantic result (filled by Stage 2)
    doc_fingerprint TEXT,           -- PDF sha256; cache-version key
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_elements_paper ON paper_elements(paper_id, kind);
"""


class Database:
    def __init__(self, db_path: str | None = None,
                 storage_context: StorageContext | None = None):
        if storage_context is not None:
            db_path = str(storage_context.metadata_db)
            if storage_context.channel == "openai_api":
                storage_context.ensure_layout()
        db_path = db_path or "data/metadata.db"
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        if storage_context is not None and storage_context.channel == "openai_api":
            storage_context.secure_private_file(db_path)

    def _migrate(self) -> None:
        """Idempotent column additions for the personal library (Phase 3.3).

        SQLite has no ADD COLUMN IF NOT EXISTS, so introspect and ALTER.
        """
        import datetime
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(papers)")}
        if "first_seen" not in cols:
            self.conn.execute("ALTER TABLE papers ADD COLUMN first_seen TEXT")
        if "last_seen" not in cols:
            self.conn.execute("ALTER TABLE papers ADD COLUMN last_seen TEXT")
        if "times_read" not in cols:
            self.conn.execute("ALTER TABLE papers ADD COLUMN times_read INTEGER DEFAULT 0")
        now = datetime.datetime.utcnow().isoformat(timespec="seconds")
        # Backfill existing rows so first_seen/last_seen are never null.
        self.conn.execute(
            "UPDATE papers SET first_seen=COALESCE(first_seen, ?), "
            "last_seen=COALESCE(last_seen, ?), times_read=COALESCE(times_read, 0) "
            "WHERE first_seen IS NULL",
            (now, now))
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS paper_library_summaries ("
            "paper_id TEXT PRIMARY KEY, summary_json TEXT NOT NULL, "
            "field_profile TEXT, read_mode TEXT, updated_at TEXT NOT NULL)")
        self.conn.commit()
    def save_paper(self, paper: Paper) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO papers
            (id, title, authors, year, venue, doi, source, language,
             citation_count, abstract, pdf_url, pdf_path, keywords, urls)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                paper.id,
                paper.title,
                json.dumps(paper.authors),
                paper.year,
                paper.venue,
                paper.doi,
                paper.source,
                paper.language,
                paper.citation_count,
                paper.abstract,
                paper.pdf_url,
                paper.pdf_path,
                json.dumps(paper.keywords),
                json.dumps(paper.urls),
            ),
        )
        self.conn.commit()

    def save_papers(self, papers: list[Paper]) -> None:
        for p in papers:
            self.save_paper(p)

    def get_paper(self, paper_id: str) -> Paper | None:
        row = self.conn.execute(
            "SELECT * FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()
        if not row:
            return None
        return _row_to_paper(row)

    def get_all_papers(self) -> list[Paper]:
        rows = self.conn.execute("SELECT * FROM papers").fetchall()
        return [_row_to_paper(r) for r in rows]

    def get_cached_summary(
        self, paper_id: str, field_profile: str, read_mode: str
    ) -> str | None:
        """Return cached summary JSON (or None) keyed by (paper_id, profile, mode)."""
        row = self.conn.execute(
            "SELECT summary_json FROM summary_cache "
            "WHERE paper_id=? AND field_profile=? AND read_mode=?",
            (paper_id, field_profile, read_mode),
        ).fetchone()
        return row["summary_json"] if row else None

    def save_cached_summary(
        self, paper_id: str, field_profile: str, read_mode: str, summary_json: str
    ) -> None:
        """Insert/replace a cached summary (idempotent on the composite key)."""
        import datetime
        self.conn.execute(
            "INSERT OR REPLACE INTO summary_cache "
            "(paper_id, field_profile, read_mode, summary_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (paper_id, field_profile, read_mode, summary_json,
             datetime.datetime.utcnow().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def delete_cached_summary(
        self, paper_id: str, field_profile: str, read_mode: str
    ) -> None:
        """Delete one summary-cache row (used to self-heal invalid full-mode rows)."""
        self.conn.execute(
            "DELETE FROM summary_cache "
            "WHERE paper_id=? AND field_profile=? AND read_mode=?",
            (paper_id, field_profile, read_mode),
        )
        self.conn.commit()

    # --- Verified full-text availability cache ---

    def get_fulltext_statuses(self, paper_ids: list[str]) -> dict[str, dict]:
        """Return persisted verification rows for a set of papers."""
        if not paper_ids:
            return {}
        placeholders = ",".join("?" * len(paper_ids))
        rows = self.conn.execute(
            f"SELECT paper_id, status, evidence, pdf_path, candidate_url, checked_at "
            f"FROM fulltext_status WHERE paper_id IN ({placeholders})",
            paper_ids,
        ).fetchall()
        return {r["paper_id"]: dict(r) for r in rows}

    def set_fulltext_status(
        self, paper_id: str, status: str, *,
        evidence: str = "", pdf_path: str = "", candidate_url: str = "",
    ) -> None:
        """Insert/replace one verification result (idempotent, safe to re-run)."""
        import datetime
        self.conn.execute(
            "INSERT OR REPLACE INTO fulltext_status "
            "(paper_id, status, evidence, pdf_path, candidate_url, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (paper_id, status, evidence, pdf_path, candidate_url,
             datetime.datetime.utcnow().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def set_fulltext_statuses(self, rows: list[tuple[str, str, str, str, str]]) -> None:
        """Bulk insert/replace of ``(paper_id, status, evidence, pdf_path, candidate_url)``."""
        if not rows:
            return
        import datetime
        now = datetime.datetime.utcnow().isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT OR REPLACE INTO fulltext_status "
            "(paper_id, status, evidence, pdf_path, candidate_url, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(pid, status, evidence, pdf_path, candidate_url, now)
             for pid, status, evidence, pdf_path, candidate_url in rows],
        )
        self.conn.commit()

    # --- Multimodal (vision) result cache ---

    def get_vision_cache(
        self, image_hash: str, task: str, prompt_version: int
    ) -> str | None:
        """Return cached vision-result JSON (or None) for this image+task+version."""
        row = self.conn.execute(
            "SELECT result_json FROM vision_cache "
            "WHERE image_hash=? AND task=? AND prompt_version=?",
            (image_hash, task, prompt_version),
        ).fetchone()
        return row["result_json"] if row else None

    def save_vision_cache(
        self, image_hash: str, task: str, prompt_version: int, result_json: str
    ) -> None:
        """Insert/replace a cached vision result (idempotent on the composite key)."""
        import datetime
        self.conn.execute(
            "INSERT OR REPLACE INTO vision_cache "
            "(image_hash, task, prompt_version, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (image_hash, task, prompt_version, result_json,
             datetime.datetime.utcnow().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    # --- Paper elements (figures / tables / formulas) ---

    def elements_fingerprint(self, paper_id: str) -> str | None:
        """The doc_fingerprint of the currently stored elements (or None)."""
        row = self.conn.execute(
            "SELECT doc_fingerprint FROM paper_elements WHERE paper_id = ? LIMIT 1",
            (paper_id,),
        ).fetchone()
        return row["doc_fingerprint"] if row else None

    def save_elements(self, paper_id: str, elements: list, doc_fingerprint: str) -> int:
        """Idempotently store a paper's elements.

        Replaces all prior elements for paper_id, then inserts. doc_fingerprint
        is stamped on every row so staleness is detectable later. Returns n.
        """
        import datetime
        now = datetime.datetime.utcnow().isoformat(timespec="seconds")
        self.conn.execute(
            "DELETE FROM paper_elements WHERE paper_id = ?", (paper_id,)
        )
        rows = []
        for el in elements:
            bbox = getattr(el, "bbox", None)
            dl = getattr(el, "docling_extract", None) or {}
            und = getattr(el, "understanding", None)
            rows.append((
                getattr(el, "element_id", "") or f"{paper_id}::unknown",
                paper_id,
                getattr(el, "kind", "") or "",
                int(getattr(el, "ordinal", 0) or 0),
                int(getattr(el, "page", 0) or 0),
                (getattr(el, "section", "") or "")[:200],
                (getattr(el, "caption", "") or "")[:1000],
                json.dumps(list(bbox)) if bbox else None,
                getattr(el, "asset_path", None),
                getattr(el, "image_hash", None),
                json.dumps(dl, ensure_ascii=False) if dl else None,
                json.dumps(und, ensure_ascii=False) if und is not None else None,
                doc_fingerprint,
                now,
            ))
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO paper_elements "
                "(element_id, paper_id, kind, ordinal, page, section, caption, "
                "bbox, asset_path, image_hash, docling_extract, understanding, "
                "doc_fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        self.conn.commit()
        return len(rows)

    def get_elements(self, paper_id: str, kind: str | None = None) -> list[dict]:
        """Return elements for a paper as dicts (bbox/docling/understanding parsed)."""
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM paper_elements WHERE paper_id=? AND kind=? "
                "ORDER BY ordinal",
                (paper_id, kind),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM paper_elements WHERE paper_id=? "
                "ORDER BY kind, ordinal",
                (paper_id,),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            for k in ("bbox", "understanding"):
                raw = d.get(k)
                d[k] = json.loads(raw) if raw else None
            d_raw = d.get("docling_extract")
            d["docling_extract"] = json.loads(d_raw) if d_raw else {}
            out.append(d)
        return out

    def delete_elements(self, paper_id: str) -> None:
        self.conn.execute(
            "DELETE FROM paper_elements WHERE paper_id = ?", (paper_id,)
        )
        self.conn.commit()

    # --- Phase 3.3: personal library (read tracking + summary persistence) ---

    def save_paper_with_read(self, paper: Paper, mark_read: bool = True) -> None:
        """Upsert paper metadata into the library, optionally bumping read stats.

        first_seen is preserved on re-save (only set on insert); last_seen and
        times_read update each time mark_read=True.
        """
        import datetime
        now = datetime.datetime.utcnow().isoformat(timespec="seconds")
        existing = self.conn.execute(
            "SELECT first_seen, times_read FROM papers WHERE id = ?",
            (paper.id,)).fetchone()
        first_seen = existing["first_seen"] if existing else now
        prev = (existing["times_read"] or 0) if existing else 0
        times_read = prev + (1 if mark_read else 0)
        self.save_paper(paper)
        self.conn.execute(
            "UPDATE papers SET first_seen=?, last_seen=?, times_read=? WHERE id=?",
            (first_seen, now, times_read, paper.id))
        self.conn.commit()

    def get_read_paper_ids(self) -> set[str]:
        """Return the set of paper ids that have been deep-read (times_read>0)."""
        rows = self.conn.execute(
            "SELECT id FROM papers WHERE times_read > 0").fetchall()
        return {r["id"] for r in rows}

    def get_read_status(self, paper_ids: list[str]) -> dict[str, dict]:
        """Read badges for a set of paper ids: {id: {times_read, last_seen}}."""
        if not paper_ids:
            return {}
        placeholders = ",".join("?" * len(paper_ids))
        rows = self.conn.execute(
            f"SELECT id, times_read, last_seen FROM papers "
            f"WHERE id IN ({placeholders}) AND times_read > 0", paper_ids).fetchall()
        return {r["id"]: {"times_read": r["times_read"] or 0,
                          "last_seen": r["last_seen"] or ""}
                for r in rows}

    def save_library_summary(
        self, paper_id: str, summary_json: str,
        field_profile: str = "", read_mode: str = "",
    ) -> None:
        """Persist a structured summary into the personal library (Phase 3.3)."""
        import datetime
        self.conn.execute(
            "INSERT OR REPLACE INTO paper_library_summaries "
            "(paper_id, summary_json, field_profile, read_mode, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (paper_id, summary_json, field_profile, read_mode,
             datetime.datetime.utcnow().isoformat(timespec="seconds")))
        self.conn.commit()

    def get_library_summary(self, paper_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT summary_json FROM paper_library_summaries WHERE paper_id = ?",
            (paper_id,)).fetchone()
        return row["summary_json"] if row else None

    def get_library(self) -> list[dict]:
        """List all library papers with read stats + whether a summary exists."""
        rows = self.conn.execute(
            "SELECT p.id, p.title, p.year, p.doi, p.first_seen, p.last_seen, p.times_read, "
            "EXISTS(SELECT 1 FROM paper_library_summaries s WHERE s.paper_id = p.id) AS has_summary "
            "FROM papers p ORDER BY p.last_seen DESC NULLS LAST").fetchall()
        return [dict(r) for r in rows]


    def close(self) -> None:
        self.conn.close()


def _row_to_paper(row: sqlite3.Row) -> Paper:
    return Paper(
        id=row["id"],
        title=row["title"],
        authors=json.loads(row["authors"]) if row["authors"] else [],
        year=row["year"],
        venue=row["venue"] or "",
        doi=row["doi"],
        source=row["source"] or "",
        language=row["language"] or "en",
        citation_count=row["citation_count"] or 0,
        abstract=row["abstract"] or "",
        pdf_url=row["pdf_url"],
        pdf_path=row["pdf_path"],
        keywords=json.loads(row["keywords"]) if row["keywords"] else [],
        urls=json.loads(row["urls"]) if row["urls"] else {},
    )
