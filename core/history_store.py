"""Unified history store (DESIGN D-071).

Single source of truth for persisting sessions, used by BOTH the conversational
agent (chat_agent / /chat/*) and the structured pipeline (/search /graph /read
/review). Replaces the two parallel stores that existed before (core/history.py
for Streamlit-era structured flow, chat_agent's own save/load/list for chat).

Design:
- One file per session under history_record/ (gitignored).
- Filename keeps a timestamp prefix for uniqueness + ordering:
      chat_<YYYYMMDD_HHMMSS>_<topic-slug>.json
  RENAMING only mutates the in-file `title` field, NEVER the filename — so
  existing references (history_filename) never break.
- `title` is the human label (editable, shown in the sidebar). Falls back to
  `topic`, then to the first user message, then "新对话".
- `trace_ids` links the session to its Trace JSONL files (D-067).
- Backward compatible: old files without title/trace_ids still load; their
  list entry derives a title from topic/timestamp.

Everything here is pure-Python filesystem ops; no network, no LLM.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = _PROJECT_ROOT / "history_record"
_FILENAME_PREFIX = "chat_"
_MAX_SLUG = 30


def _record_dir() -> Path:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return HISTORY_DIR


def _slugify(text: str) -> str:
    """Filename-safe slug preserving CJK + alnum (no ascii-folding)."""
    text = (text or "").strip()
    # keep word chars (unicode, so CJK passes) and spaces; drop punctuation
    kept = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    kept = re.sub(r"\s+", "_", kept).strip("_")
    return kept[:_MAX_SLUG] or "untitled"


def _new_filename(topic: str, record_dir: Path) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{_FILENAME_PREFIX}{ts}_{_slugify(topic)}"
    name = f"{base}.json"
    # Two records created in the same second with the same slug must never
    # collide (e.g. two users starting unnamed chats concurrently).
    if (record_dir / name).exists():
        import uuid
        name = f"{base}_{uuid.uuid4().hex[:6]}.json"
    return name


def _resolve(filename: str) -> Path:
    """Resolve a bare filename (or path) to a Path under HISTORY_DIR.

    Strips any directory component to prevent path traversal.
    """
    bare = Path(filename).name
    return _record_dir() / bare


def derive_title(data: dict[str, Any]) -> str:
    """Best-effort human title from a session dict (D-071)."""
    if data.get("title"):
        return str(data["title"]).strip()
    if data.get("topic"):
        return str(data["topic"]).strip()
    msgs = data.get("messages") or []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            content = str(m.get("content", "")).strip()
            if content:
                # first line, truncated
                return content.split("\n", 1)[0][:40]
    return "新对话"


def _derive_source(data: dict[str, Any]) -> str:
    """Derive source label from actual content.

    'chat' = conversation only; 'structured'/'both' kept for legacy records
    (the structured pipeline no longer exists). Records with research
    artifacts (summaries / review / research map) count as pipeline output.
    """
    has_chat = len(data.get("messages") or []) > 0
    has_search = len(data.get("papers") or []) > 0
    has_pipeline_output = bool(
        data.get("paper_summaries")
        or data.get("literature_review")
        or data.get("map_data")
        or data.get("graph_data")          # legacy records
        or data.get("paper_framework")     # legacy records
        or data.get("research_directions")  # legacy records
    )
    if has_chat and has_pipeline_output:
        return "both"
    if has_pipeline_output:
        return "structured"
    if has_chat:
        return "chat"
    return "structured" if has_search else "chat"


def _owner_of(data: dict[str, Any]) -> str:
    """Record owner. Legacy records without user_id belong to the local user."""
    return str(data.get("user_id") or "local")


def save_session(data: dict[str, Any], filename: str | None = None,
                 user_id: str = "local") -> str:
    """Persist `data` as one session record. Returns the bare filename.

    If `filename` is given, updates that record in place (merging: keep
    existing title/trace_ids unless overridden). Otherwise creates a new
    timestamped file. Always refreshes `timestamp_updated`. `user_id` stamps
    the record's owner (multi-user isolation); an existing record owned by a
    different user is NOT touched — a new record is created instead.
    """
    record_dir = _record_dir()

    if filename:
        fp = _resolve(filename)
        existing: dict[str, Any] = {}
        if fp.exists():
            try:
                existing = json.loads(fp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
        if existing and _owner_of(existing) != user_id:
            # Never overwrite another user's record; fork to a fresh file.
            fp = record_dir / _new_filename(data.get("topic") or data.get("title") or "",
                                            record_dir)
            existing = {}
    else:
        fp = record_dir / _new_filename(data.get("topic") or data.get("title") or "",
                                        record_dir)
        existing = {}

    merged = dict(existing)
    merged.update(data)
    merged["user_id"] = user_id
    # title: keep existing if data didn't carry one and no topic/title in data
    if not merged.get("title"):
        merged["title"] = derive_title(merged)
    # preserve trace_ids accumulation if caller didn't pass it
    if "trace_ids" not in data and existing.get("trace_ids"):
        merged["trace_ids"] = existing["trace_ids"]
    if not merged.get("timestamp"):
        merged["timestamp"] = datetime.now().strftime("%Y%m%d_%H%M%S")
    merged["timestamp_updated"] = datetime.now().strftime("%Y%m%d_%H%M%S")

    fp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp.name


def load_session(filename: str, user_id: str | None = None) -> dict[str, Any] | None:
    """Load a session record by bare filename. None if missing/unreadable.

    When `user_id` is given, records owned by a different user read as
    not-found (None) — cross-account access is indistinguishable from a bad
    filename.

    Security (D-089): `filename` is ALWAYS reduced to its basename via
    `_resolve`, so path separators (`/`, `..`, absolute paths) cannot escape
    HISTORY_DIR. The old fallback that opened any existing `Path(filename)`
    on the host was an arbitrary-file-read vector reachable from
    `GET /history/{filename:path}` — it let an attacker read any JSON file on
    the machine. All legitimate callers pass a bare filename (or an absolute
    path inside HISTORY_DIR whose basename `_resolve` already extracts), so
    the fallback is removed.
    """
    fp = _resolve(filename)
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if user_id is not None and _owner_of(data) != user_id:
        return None
    if not data.get("title"):
        data["title"] = derive_title(data)
    return data


def list_sessions(user_id: str | None = None) -> list[dict[str, Any]]:
    """List session records, newest by mtime first. `user_id` filters to one
    owner's records (None = list everything, internal/legacy use)."""
    record_dir = _record_dir()
    # D-079: glob ALL *.json so structured records (no chat_ prefix) appear in
    # both the structured and chat sidebars; the source badge distinguishes them.
    files = sorted(record_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if user_id is not None and _owner_of(data) != user_id:
            continue
        out.append({
            "filename": fp.name,
            "timestamp": data.get("timestamp", ""),
            "timestamp_updated": data.get("timestamp_updated", data.get("timestamp", "")),
            "title": data.get("title") or derive_title(data),
            "topic": data.get("topic", ""),
            "message_count": len(data.get("messages", [])),
            "paper_count": len(data.get("papers", [])),
            "has_review": bool(data.get("literature_review")),
            "has_map": bool(data.get("map_data") or data.get("graph_data")),
            "trace_ids": data.get("trace_ids", []),
        })
        out[-1]["source"] = _derive_source(data)
    return out


def rename_session(filename: str, new_title: str,
                   user_id: str | None = None) -> dict[str, Any] | None:
    """Rename a session's title in place (filename unchanged). Returns updated record or None."""
    title = (new_title or "").strip()
    if not title:
        return None
    fp = _resolve(filename)
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if user_id is not None and _owner_of(data) != user_id:
        return None
    data["title"] = title
    data["timestamp_updated"] = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def delete_session(filename: str, user_id: str | None = None) -> bool:
    """Delete a session record. Returns True if removed. With `user_id`,
    another owner's record is treated as not-found (not deleted)."""
    fp = _resolve(filename)
    if fp.exists():
        if user_id is not None:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            if _owner_of(data) != user_id:
                return False
        try:
            fp.unlink()
            return True
        except OSError:
            return False
    return False


def add_trace_id(filename: str, trace_id: str) -> None:
    """Append a trace_id to a session's trace_ids list (D-067 linkage)."""
    if not trace_id:
        return
    fp = _resolve(filename)
    if not fp.exists():
        return
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    ids = list(data.get("trace_ids") or [])
    if trace_id not in ids:
        ids.append(trace_id)
        data["trace_ids"] = ids
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
