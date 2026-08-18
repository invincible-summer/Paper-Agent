"""Local FTS5 catalogue populated exclusively from the official Rxiv API."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from core.models import Paper
from tools.search.base import generate_paper_id

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = _PROJECT_ROOT / "data" / "paper_source_catalog.db"
START_DATES = {"biorxiv": date(2013, 11, 1), "medrxiv": date(2019, 6, 1)}


def _connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or _DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS rxiv_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server TEXT NOT NULL CHECK(server IN ('biorxiv','medrxiv')),
        doi TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        title TEXT NOT NULL,
        authors_json TEXT NOT NULL DEFAULT '[]',
        author_text TEXT NOT NULL DEFAULT '',
        abstract TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        published TEXT NOT NULL DEFAULT '',
        updated TEXT NOT NULL DEFAULT '',
        license TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL,
        synced_at REAL NOT NULL,
        UNIQUE(server, doi)
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS rxiv_fts USING fts5(
        title, abstract, author_text, category,
        tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TABLE IF NOT EXISTS rxiv_sync_state (
        server TEXT PRIMARY KEY,
        backfill_date TEXT NOT NULL,
        backfill_cursor INTEGER NOT NULL DEFAULT 0,
        last_success_at REAL,
        last_error TEXT,
        indexed_count INTEGER NOT NULL DEFAULT 0,
        min_published TEXT,
        max_published TEXT
    );
    """)
    state_columns = {row[1] for row in conn.execute("PRAGMA table_info(rxiv_sync_state)")}
    if "backfill_cursor" not in state_columns:
        conn.execute("ALTER TABLE rxiv_sync_state ADD COLUMN backfill_cursor INTEGER NOT NULL DEFAULT 0")
    return conn


def _authors(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in re.split(r";|,", str(value or "")) if part.strip()]


def upsert_records(server: str, records: list[dict], *, path: Path | None = None) -> int:
    if server not in START_DATES:
        raise ValueError("invalid rxiv server")
    conn = _connect(path)
    changed = 0
    try:
        for item in records:
            doi = str(item.get("doi") or "").strip().lower()
            title = str(item.get("title") or "").strip()
            if not doi or not title:
                continue
            authors = _authors(item.get("authors") or item.get("author_corresponding"))
            version_raw = item.get("version") or 1
            try: version = int(version_raw)
            except (TypeError, ValueError): version = 1
            existing = conn.execute(
                "SELECT id, version FROM rxiv_records WHERE server=? AND doi=?", (server, doi)
            ).fetchone()
            if existing and int(existing["version"]) >= version:
                continue
            host = "www.biorxiv.org" if server == "biorxiv" else "www.medrxiv.org"
            official_url = f"https://{host}/content/{doi}v{version}"
            values = (
                server, doi, version, title, json.dumps(authors, ensure_ascii=False),
                " ".join(authors), str(item.get("abstract") or "").strip(),
                str(item.get("category") or "").strip(), str(item.get("date") or "").strip(),
                str(item.get("published") or item.get("date") or "").strip(),
                str(item.get("license") or "").strip(),
                official_url, time.time(),
            )
            if existing:
                rowid = int(existing["id"])
                conn.execute("DELETE FROM rxiv_fts WHERE rowid=?", (rowid,))
                conn.execute("""UPDATE rxiv_records SET version=?,title=?,authors_json=?,author_text=?,abstract=?,category=?,published=?,updated=?,license=?,url=?,synced_at=? WHERE id=?""",
                             (version, title, values[4], values[5], values[6], values[7], values[8], values[9], values[10], values[11], values[12], rowid))
            else:
                cur = conn.execute("""INSERT INTO rxiv_records(server,doi,version,title,authors_json,author_text,abstract,category,published,updated,license,url,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                rowid = int(cur.lastrowid)
            conn.execute("INSERT INTO rxiv_fts(rowid,title,abstract,author_text,category) VALUES(?,?,?,?,?)",
                         (rowid, title, values[6], values[5], values[7]))
            changed += 1
        conn.commit()
        return changed
    finally:
        conn.close()


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[\w\u3400-\u9fff]+", query, flags=re.UNICODE)[:12]
    return " AND ".join(f'"{token}"' for token in tokens) or '""'


def search_catalog(server: str, query: str, limit: int = 20, *, path: Path | None = None) -> list[Paper]:
    conn = _connect(path)
    try:
        rows = conn.execute("""
            SELECT r.*, bm25(rxiv_fts, 5.0, 2.0, 1.0, 1.0) AS score
            FROM rxiv_fts JOIN rxiv_records r ON r.id=rxiv_fts.rowid
            WHERE rxiv_fts MATCH ? AND r.server=? ORDER BY score LIMIT ?
        """, (_fts_query(query), server, min(limit, 100))).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    papers = []
    for row in rows:
        authors = json.loads(row["authors_json"] or "[]")
        year = int(row["published"][:4]) if str(row["published"])[:4].isdigit() else None
        doi = row["doi"]
        papers.append(Paper(
            id=generate_paper_id(row["title"], authors[0] if authors else "", year, doi),
            title=row["title"], authors=authors, year=year, venue=server,
            doi=doi, source=server, abstract=row["abstract"], pdf_url=None,
            keywords=[row["category"]] if row["category"] else [], urls={server: row["url"]},
        ))
    return papers


def update_sync_state(server: str, *, next_date: date | None = None,
                      cursor: int | None = None, error: str | None = None,
                      path: Path | None = None) -> None:
    conn = _connect(path)
    try:
        current = conn.execute(
            "SELECT backfill_date,backfill_cursor FROM rxiv_sync_state WHERE server=?", (server,)
        ).fetchone()
        backfill = (next_date or (date.fromisoformat(current[0]) if current else START_DATES[server])).isoformat()
        next_cursor = max(0, int(cursor if cursor is not None else (current[1] if current else 0)))
        stats = conn.execute("SELECT COUNT(*),MIN(published),MAX(published) FROM rxiv_records WHERE server=?", (server,)).fetchone()
        conn.execute("""INSERT INTO rxiv_sync_state(server,backfill_date,backfill_cursor,last_success_at,last_error,indexed_count,min_published,max_published)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(server) DO UPDATE SET backfill_date=excluded.backfill_date,
            backfill_cursor=excluded.backfill_cursor,
            last_success_at=excluded.last_success_at,last_error=excluded.last_error,indexed_count=excluded.indexed_count,
            min_published=excluded.min_published,max_published=excluded.max_published""",
            (server, backfill, next_cursor, None if error else time.time(), error, int(stats[0]), stats[1], stats[2]))
        conn.commit()
    finally:
        conn.close()


def sync_status(server: str | None = None, *, path: Path | None = None) -> dict:
    conn = _connect(path)
    try:
        rows = conn.execute("SELECT * FROM rxiv_sync_state" + (" WHERE server=?" if server else ""),
                            ((server,) if server else ())).fetchall()
        result = {row["server"]: dict(row) for row in rows}
        for name in START_DATES:
            result.setdefault(name, {"server": name, "backfill_date": START_DATES[name].isoformat(),
                                     "backfill_cursor": 0,
                                     "last_success_at": None, "last_error": None, "indexed_count": 0,
                                     "min_published": None, "max_published": None})
        return result[server] if server else result
    finally:
        conn.close()
