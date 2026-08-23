"""One-time retirement of legacy *network-paper* full-text caches.

The current product never fetches network-paper PDFs.  Older installations may
still contain rebuildable PDFs, element crops, full-mode summaries/status rows,
vector chunks, and checkpoint fields produced by that retired path.  This
module removes only those derived network artifacts.  User uploads, upload
sidecars, ``upload:*`` elements/vectors, message history, and existing exports
are explicitly preserved.

The operation is idempotent and safe to preview.  ``dry_run=True`` is the
library default; the backend startup hook passes ``dry_run=False`` once and
records a version marker after a successful pass.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import zlib
import copy
import hashlib
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

RETIREMENT_VERSION = 2
MARKER_NAME = ".remote_fulltext_retirement_v2.json"


def _root(value: str | os.PathLike[str] | None, fallback: Path) -> Path:
    return Path(value).expanduser().resolve() if value else fallback.resolve()


def _is_upload_id(value: Any, upload_ids: set[str] | None = None) -> bool:
    text = str(value or "")
    return text.startswith("upload:") or (upload_ids is not None and text in upload_ids)


def _safe_unlink(path: Path, dry_run: bool) -> tuple[int, int]:
    try:
        if not path.is_file() or path.is_symlink():
            if path.is_symlink() and not dry_run:
                path.unlink(missing_ok=True)
            return (0, 0)
        size = int(path.stat().st_size)
    except OSError:
        return (0, 0)
    if not dry_run:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return (0, 0)
    return (1, size)


def _walk_files(directory: Path) -> Iterable[Path]:
    if not directory.is_dir():
        return ()
    try:
        return (p for p in directory.rglob("*") if p.is_file() or p.is_symlink())
    except OSError:
        return ()


def _remove_empty_dirs(directory: Path, dry_run: bool) -> None:
    if dry_run or not directory.is_dir():
        return
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _collect_upload_ids(root: Path) -> set[str]:
    """Collect old and current upload namespaces without opening upload bytes."""
    ids: set[str] = set()
    history_dirs = [root / "history_record", root / "backend" / "history_record"]
    for directory in history_dirs:
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            data = _load_json(path)
            if not data:
                continue
            for attachment in data.get("attachments") or []:
                if isinstance(attachment, dict) and attachment.get("id"):
                    aid = str(attachment["id"])
                    ids.add(aid)
                    ids.add(f"upload:{aid}")
            for summary_id in (data.get("paper_summaries") or {}):
                sid = str(summary_id)
                if sid.startswith("upload:"):
                    ids.add(sid)
    for db_path in (root / "data" / "metadata.db", root / "backend" / "data" / "metadata.db"):
        if not db_path.is_file():
            continue
        try:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute("SELECT id FROM papers WHERE source='upload' OR id LIKE 'upload:%'").fetchall()
                ids.update(str(row[0]) for row in rows if row[0])
        except sqlite3.Error:
            continue
    return ids


def _network_full_ids(db_path: Path) -> set[str]:
    """Capture ids whose old summary/status was full-text derived."""
    ids: set[str] = set()
    if not db_path.is_file():
        return ids
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT paper_id, read_mode, summary_json FROM summary_cache"
                ).fetchall()
                for row in rows:
                    pid = str(row[0] or "")
                    if not pid:
                        continue
                    if str(row[0] or "") and str(row[1] or ""):
                        try:
                            summary = json.loads(row[2])
                        except (TypeError, ValueError, json.JSONDecodeError):
                            summary = {}
                    else:
                        summary = {}
                    if str(row[0] or "") and (
                        str(row[1] or "").lower() in {"full", "fulltext"}
                        or (isinstance(summary, dict) and (
                            summary.get("full_text") or
                            (summary.get("document_info") or {}).get("read_level") == "full"
                        ))
                    ):
                        ids.add(pid)
            except sqlite3.Error:
                pass
            try:
                rows = conn.execute("SELECT paper_id FROM fulltext_status").fetchall()
                ids.update(str(row[0]) for row in rows if row[0])
            except sqlite3.Error:
                pass
    except sqlite3.Error:
        pass
    return {pid for pid in ids if not pid.startswith("upload:")}


def _summary_cache_is_full(read_mode: Any, summary_json: Any) -> bool:
    if str(read_mode or "").lower() in {"full", "fulltext"}:
        return True
    try:
        summary = json.loads(summary_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        summary = {}
    if not isinstance(summary, dict):
        return False
    info = summary.get("document_info") or {}
    return bool(
        summary.get("full_text")
        or summary.get("elements")
        or (isinstance(info, dict) and str(info.get("read_level") or "").lower() == "full")
    )


def _scrub_network_summary(summary: Any) -> tuple[Any | None, bool]:
    if not isinstance(summary, dict):
        return summary, False
    info = summary.get("document_info") or {}
    read_level = str(info.get("read_level") or "").lower() if isinstance(info, dict) else ""
    # A full-mode row is derived from the remote document and must not survive.
    if summary.get("full_text") or read_level == "full" or summary.get("elements"):
        return None, True
    changed = False
    for key in ("full_text", "embedding", "elements", "section_outline", "document_info"):
        if key in summary:
            summary.pop(key, None)
            changed = True
    return summary, changed


def _is_upload_record(value: dict[str, Any], upload_ids: set[str]) -> bool:
    identifier = value.get("id") or value.get("paper_id") or value.get("attachment_id")
    return (
        str(value.get("source") or "").lower() == "upload"
        or _is_upload_id(identifier, upload_ids)
    )


def _looks_like_paper_record(value: dict[str, Any]) -> bool:
    if not (value.get("id") or value.get("paper_id")):
        return False
    return bool({
        "title", "authors", "abstract", "doi", "venue", "year", "source",
        "citation_count", "pdf_path", "pdf_url", "pdf_source", "fulltext_status",
    } & set(value))


def scrub_history_data(
    data: dict[str, Any], upload_ids: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Remove legacy network-fulltext state from nested history/checkpoints.

    Message text and upload-owned records remain untouched.  Old tool results
    may duplicate paper dictionaries several levels below ``messages``; the
    recursive walk is therefore deliberate rather than limited to top-level
    session fields.
    """
    result = copy.deepcopy(data)
    protected_upload_ids = set(upload_ids or ())
    stats = {"history_fields_removed": 0, "history_summaries_removed": 0}
    retired_state_fields = {
        "pdf_paths", "fulltext_fallbacks", "fulltext_status",
        "fulltext_statuses", "fulltext_core_available", "fulltext_core_target",
        "fulltext_core_reserved", "remote_fetch_blocked",
    }
    retired_paper_fields = {
        "pdf_path", "pdf_url", "pdf_source", "fulltext_status", "fulltext",
    }

    def scrub_summaries(value: dict[Any, Any]) -> dict[Any, Any]:
        cleaned_summaries: dict[Any, Any] = {}
        for raw_id, summary in value.items():
            paper_id = str(raw_id)
            summary_id = summary.get("paper_id") if isinstance(summary, dict) else None
            if _is_upload_id(paper_id, protected_upload_ids) or _is_upload_id(
                summary_id, protected_upload_ids
            ):
                cleaned_summaries[raw_id] = summary
                continue
            cleaned, changed = _scrub_network_summary(summary)
            if cleaned is None:
                stats["history_summaries_removed"] += 1
                continue
            cleaned_summaries[raw_id] = cleaned
            if changed:
                stats["history_fields_removed"] += 1
        return cleaned_summaries

    def walk(value: Any) -> Any:
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value

        item = dict(value)
        is_upload = _is_upload_record(item, protected_upload_ids)
        for field in tuple(retired_state_fields):
            if field in item:
                item.pop(field, None)
                stats["history_fields_removed"] += 1
        if _looks_like_paper_record(item) and not is_upload:
            for field in retired_paper_fields:
                if field in item:
                    item.pop(field, None)
                    stats["history_fields_removed"] += 1

        for key, nested in tuple(item.items()):
            if key == "paper_summaries" and isinstance(nested, dict):
                item[key] = scrub_summaries(nested)
            else:
                item[key] = walk(nested)
        return item

    return walk(result), stats

def scrub_history_file(
    path: Path, *, dry_run: bool = False, upload_ids: set[str] | None = None,
) -> dict[str, int]:
    data = _load_json(path)
    if data is None:
        return {"history_files_seen": 1, "history_files_changed": 0, "history_fields_removed": 0, "history_summaries_removed": 0}
    cleaned, stats = scrub_history_data(data, upload_ids)
    changed = cleaned != data
    if changed and not dry_run:
        _write_json(path, cleaned)
    return {"history_files_seen": 1, "history_files_changed": int(changed), **stats}


def _clean_metadata_db(path: Path, upload_ids: set[str], dry_run: bool, stats: dict[str, int]) -> set[str]:
    full_ids = _network_full_ids(path)
    if not path.is_file():
        return full_ids
    try:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            rows = conn.execute(
                "SELECT rowid, paper_id, read_mode, summary_json FROM summary_cache"
            ).fetchall()
            ids = [
                int(row[0]) for row in rows
                if not _is_upload_id(row[1], upload_ids)
                and _summary_cache_is_full(row[2], row[3])
            ]
            stats["db_rows_removed"] += len(ids)
            if ids and not dry_run:
                conn.executemany("DELETE FROM summary_cache WHERE rowid=?", [(item,) for item in ids])
        except sqlite3.Error:
            pass
        try:
            rows = conn.execute("SELECT rowid, paper_id FROM fulltext_status").fetchall()
            ids = [int(row[0]) for row in rows if not _is_upload_id(row[1], upload_ids)]
            stats["db_rows_removed"] += len(ids)
            if ids and not dry_run:
                conn.executemany("DELETE FROM fulltext_status WHERE rowid=?", [(item,) for item in ids])
        except sqlite3.Error:
            pass
        try:
            # The runtime schema no longer recreates this compatibility table.
            stats["db_rows_removed"] += int(conn.execute("SELECT COUNT(*) FROM fulltext_status").fetchone()[0])
            if not dry_run:
                conn.execute("DROP TABLE IF EXISTS fulltext_status")
        except sqlite3.Error:
            pass
        try:
            # Compatibility schemas differ across old installations (some do
            # not have ``pdf_source`` and some store an upload id without the
            # ``upload:`` prefix).  Select rows first and update only columns
            # that actually exist, protecting every known upload id.
            paper_columns = {row[1] for row in conn.execute("PRAGMA table_info(papers)").fetchall()}
            path_columns = [
                column for column in ("pdf_path", "pdf_url", "pdf_source")
                if column in paper_columns
            ]
            if path_columns:
                selected = conn.execute(
                    "SELECT id, source, " + ", ".join(path_columns) + " FROM papers"
                ).fetchall()
                clear_ids = []
                for row in selected:
                    paper_id, source = row[0], row[1]
                    if _is_upload_id(paper_id, upload_ids) or str(source or "").lower() == "upload":
                        continue
                    if any(row[index + 2] for index in range(len(path_columns))):
                        clear_ids.append(paper_id)
                stats["db_paper_paths_cleared"] += len(clear_ids)
                if clear_ids and not dry_run:
                    assignments = ", ".join(f"{column}=NULL" for column in path_columns)
                    conn.executemany(
                        f"UPDATE papers SET {assignments} WHERE id=?",
                        [(paper_id,) for paper_id in clear_ids],
                    )
        except sqlite3.Error:
            pass
        try:
            element_rows = conn.execute(
                "SELECT rowid, paper_id FROM paper_elements"
            ).fetchall()
            remove_element_rows = [
                int(row[0]) for row in element_rows
                if not _is_upload_id(row[1], upload_ids)
            ]
            stats["db_elements_removed"] += len(remove_element_rows)
            if remove_element_rows and not dry_run:
                conn.executemany(
                    "DELETE FROM paper_elements WHERE rowid=?",
                    [(rowid,) for rowid in remove_element_rows],
                )
        except sqlite3.Error:
            pass
        if not dry_run:
            conn.commit()
            try:
                conn.execute("VACUUM")
            except sqlite3.Error:
                pass
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("remote cache metadata cleanup skipped for %s: %s", path, exc)
    return full_ids


def _clean_web_files(root: Path, upload_asset_paths: set[Path], dry_run: bool, stats: dict[str, int]) -> None:
    for directory in (root / "data" / "pdfs",):
        for path in _walk_files(directory):
            files, size = _safe_unlink(path, dry_run)
            stats["network_files_removed"] += files
            stats["network_bytes_removed"] += size
        _remove_empty_dirs(directory, dry_run)
    assets = root / "data" / "assets"
    for path in _walk_files(assets):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in upload_asset_paths or any(part.startswith("upload") for part in path.parts):
            continue
        files, size = _safe_unlink(path, dry_run)
        stats["network_files_removed"] += files
        stats["network_bytes_removed"] += size
    _remove_empty_dirs(assets, dry_run)


def _asset_paths_from_db(db_path: Path, root: Path) -> set[Path]:
    protected: set[Path] = set()
    if not db_path.is_file():
        return protected
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT asset_path FROM paper_elements WHERE paper_id LIKE 'upload:%'").fetchall()
        for (raw,) in rows:
            if not raw:
                continue
            p = Path(str(raw))
            if not p.is_absolute():
                p = root / p
            try:
                protected.add(p.resolve())
            except OSError:
                protected.add(p)
    except sqlite3.Error:
        pass
    return protected


def _clean_chroma(path: Path, upload_ids: set[str], network_full_ids: set[str], dry_run: bool, stats: dict[str, int]) -> None:
    if not path.is_dir():
        return
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(path))
    except Exception as exc:  # no Chroma install/corrupt old store: files remain for explicit audit
        logger.info("Chroma cleanup skipped for %s: %s", path, exc)
        return

    def clean_collection(name: str, predicate) -> None:
        try:
            coll = client.get_collection(name)
            data = coll.get(include=["metadatas"])
            ids = data.get("ids") or []
            metas = data.get("metadatas") or []
            remove = [str(item_id) for item_id, meta in zip(ids, metas) if predicate(meta or {})]
            stats["vector_records_removed"] += len(remove)
            if remove and not dry_run:
                for start in range(0, len(remove), 500):
                    coll.delete(ids=remove[start:start + 500])
        except Exception as exc:  # noqa: BLE001
            logger.info("Chroma collection %s cleanup skipped: %s", name, exc)

    def is_network_chunk(meta: dict) -> bool:
        pid = str(meta.get("paper_id") or "")
        if _is_upload_id(pid, upload_ids):
            return False
        return bool(pid)

    def is_network_summary(meta: dict) -> bool:
        return str(meta.get("paper_id") or "") in network_full_ids

    clean_collection("fulltext_chunks", is_network_chunk)
    clean_collection("summaries", is_network_summary)
    clean_collection("elements", lambda meta: not _is_upload_id(meta.get("paper_id"), upload_ids) and bool(meta.get("paper_id")))


def _api_attachment_ids_from_checkpoints(db_path: Path) -> set[str]:
    ids: set[str] = set()
    if not db_path.is_file():
        return ids
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT checkpoint_blob FROM api_sessions WHERE checkpoint_blob IS NOT NULL").fetchall()
        for (blob,) in rows:
            try:
                state = json.loads(zlib.decompress(blob).decode("utf-8"))
                for attachment in state.get("attachments") or []:
                    if isinstance(attachment, dict) and attachment.get("id"):
                        aid = str(attachment["id"])
                        ids.update((aid, f"upload:{aid}"))
            except (ValueError, TypeError, zlib.error, UnicodeDecodeError, json.JSONDecodeError):
                continue
    except sqlite3.Error:
        pass
    return ids


def _api_attachment_ids_from_payloads(api_root: Path) -> set[str]:
    ids: set[str] = set()
    db_path = api_root / "state.db"
    if not db_path.is_file():
        return ids
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT relative_path FROM api_artifacts "
                "WHERE category='state_payload' AND status='active'"
            ).fetchall()
        for (relative,) in rows:
            try:
                blob = (api_root / str(relative)).resolve().read_bytes()
                state = json.loads(zlib.decompress(blob).decode("utf-8"))
            except (OSError, ValueError, TypeError, zlib.error, UnicodeDecodeError, json.JSONDecodeError):
                continue
            for attachment in (state.get("attachments") or []) if isinstance(state, dict) else ():
                if isinstance(attachment, dict) and attachment.get("id"):
                    aid = str(attachment["id"])
                    ids.update((aid, f"upload:{aid}"))
    except sqlite3.Error:
        pass
    return ids


def _scrub_checkpoint_blob(
    blob: bytes, upload_ids: set[str] | None = None,
) -> tuple[bytes, bool]:
    try:
        state = json.loads(zlib.decompress(blob).decode("utf-8"))
    except (ValueError, TypeError, zlib.error, UnicodeDecodeError, json.JSONDecodeError):
        return blob, False
    if not isinstance(state, dict):
        return blob, False
    cleaned, _ = scrub_history_data(state, upload_ids)
    cleaned.pop("review_metadata", None)  # may contain legacy source refs
    encoded = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    original = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return zlib.compress(encoded, level=6), encoded != original


def _scrub_external_value(
    blob: bytes, field: str, upload_ids: set[str] | None = None,
) -> tuple[bytes, bool]:
    """Scrub an externalized checkpoint field without assuming a full state dict."""
    try:
        value = json.loads(zlib.decompress(blob).decode("utf-8"))
    except (ValueError, TypeError, zlib.error, UnicodeDecodeError, json.JSONDecodeError):
        return blob, False
    original = copy.deepcopy(value)
    if isinstance(value, dict) and {"papers", "candidates", "paper_summaries", "attachments"} & set(value):
        value, _ = scrub_history_data(value, upload_ids)
    elif field == "paper_summaries" and isinstance(value, dict):
        cleaned = {}
        for paper_id, summary in value.items():
            if _is_upload_id(paper_id, upload_ids):
                cleaned[paper_id] = summary
                continue
            scrubbed, _ = _scrub_network_summary(summary)
            if scrubbed is not None:
                cleaned[paper_id] = scrubbed
        value = cleaned
    elif field in {"papers", "candidates"} and isinstance(value, list):
        value, _ = scrub_history_data({field: value}, upload_ids)
        value = value.get(field, [])
    elif field == "map_data" and isinstance(value, dict):
        value, _ = scrub_history_data({"map_data": value}, upload_ids)
        value = value.get("map_data", {})
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    before = json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return zlib.compress(encoded, level=6), encoded != before


def _clean_api_root(api_root: Path, upload_ids: set[str], dry_run: bool, stats: dict[str, int]) -> None:
    db_path = api_root / "state.db"
    if db_path.is_file():
        upload_ids.update(_api_attachment_ids_from_checkpoints(db_path))
        upload_ids.update(_api_attachment_ids_from_payloads(api_root))
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT id, checkpoint_blob FROM api_sessions WHERE checkpoint_blob IS NOT NULL").fetchall()
            for row in rows:
                new_blob, changed = _scrub_checkpoint_blob(
                    bytes(row["checkpoint_blob"]), upload_ids
                )
                if changed:
                    stats["checkpoint_blobs_changed"] += 1
                    if not dry_run:
                        conn.execute("UPDATE api_sessions SET checkpoint_blob=?, checkpoint_uncompressed_bytes=? WHERE id=?", (new_blob, len(zlib.decompress(new_blob)), row["id"]))
            # Remote public PDFs and public element assets are rebuildable.
            rows = conn.execute("SELECT id, relative_path, category, scope FROM api_artifacts WHERE status='active' AND ((category='public_pdf') OR (category='element_asset' AND scope='public'))").fetchall()
            for row in rows:
                try:
                    path = (api_root / str(row["relative_path"])).resolve()
                    if path.is_file():
                        files, size = _safe_unlink(path, dry_run)
                        stats["api_artifacts_removed"] += files
                        stats["api_bytes_removed"] += size
                except OSError:
                    pass
                if not dry_run:
                    conn.execute("UPDATE api_artifacts SET status='deleted', public_alias=NULL WHERE id=?", (row["id"],))
            # Older checkpoints externalized large fields into zlib JSON
            # artifacts.  Rewrite those payloads with the same scrubber and
            # retain the artifact/session relation and private ownership.
            payload_rows = conn.execute(
                "SELECT id, relative_path, size_bytes, logical_name FROM api_artifacts "
                "WHERE status='active' AND category='state_payload'"
            ).fetchall()
            for row in payload_rows:
                try:
                    payload_path = (api_root / str(row["relative_path"])).resolve()
                    old_blob = payload_path.read_bytes()
                    new_blob, changed = _scrub_external_value(
                        old_blob, str(row["logical_name"] or ""), upload_ids
                    )
                except (OSError, ValueError):
                    continue
                if not changed:
                    continue
                stats["checkpoint_blobs_changed"] += 1
                if not dry_run:
                    payload_path.write_bytes(new_blob)
                    conn.execute(
                        "UPDATE api_artifacts SET content_hash=?, size_bytes=? WHERE id=?",
                        (hashlib.sha256(new_blob).hexdigest(), len(new_blob), row["id"]),
                    )
            if not dry_run:
                conn.commit()
                try:
                    conn.execute("VACUUM")
                except sqlite3.Error:
                    pass
            conn.close()
        except sqlite3.Error as exc:
            logger.warning("API state cleanup skipped for %s: %s", db_path, exc)
    metadata = api_root / "metadata.db"
    full_ids = _clean_metadata_db(metadata, upload_ids, dry_run, stats)
    _clean_chroma(api_root / "chroma", upload_ids, full_ids, dry_run, stats)
    # Do not traverse blobs/upload or blobs/upload_sidecar.  Orphan public PDF
    # files are covered by the artifact rows above; unknown files are private.


def retire_remote_fulltext(
    project_root: str | os.PathLike[str] | None = None,
    *, api_roots: Iterable[str | os.PathLike[str]] | None = None,
    dry_run: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Preview or execute the idempotent retirement migration."""
    root = _root(project_root, Path(__file__).resolve().parents[1])
    marker = root / "data" / MARKER_NAME
    if not dry_run and marker.is_file() and not force:
        prior = _load_json(marker) or {}
        return {"version": RETIREMENT_VERSION, "status": "already_applied", "marker": str(marker), "prior": prior}

    stats: dict[str, int] = {
        "network_files_removed": 0, "network_bytes_removed": 0,
        "db_rows_removed": 0, "db_paper_paths_cleared": 0, "db_elements_removed": 0,
        "vector_records_removed": 0, "checkpoint_blobs_changed": 0,
        "api_artifacts_removed": 0, "api_bytes_removed": 0,
        "history_files_seen": 0, "history_files_changed": 0,
        "history_fields_removed": 0, "history_summaries_removed": 0,
    }
    upload_ids = _collect_upload_ids(root)
    network_full_ids: set[str] = set()
    for base in (root, root / "backend"):
        for history_file in (base / "history_record").glob("*.json") if (base / "history_record").is_dir() else ():
            item = scrub_history_file(
                history_file, dry_run=dry_run, upload_ids=upload_ids
            )
            for key, value in item.items():
                stats[key] = stats.get(key, 0) + value
        db_path = base / "data" / "metadata.db"
        network_full_ids.update(_network_full_ids(db_path))
        protected = _asset_paths_from_db(db_path, base)
        _clean_metadata_db(db_path, upload_ids, dry_run, stats)
        _clean_web_files(base, protected, dry_run, stats)
        _clean_chroma(base / "data" / "chroma", upload_ids, network_full_ids, dry_run, stats)

    roots: list[Path] = []
    configured = os.getenv("OPENAI_API_STORAGE_ROOT")
    if configured:
        roots.append(Path(configured).expanduser().resolve())
    roots.extend([root / "data" / "openai_api", root / "backend" / "data" / "openai_api"])
    roots.extend(_root(item, root) for item in (api_roots or ()))
    seen: set[Path] = set()
    for api_root in roots:
        api_root = api_root.resolve()
        if api_root in seen:
            continue
        seen.add(api_root)
        _clean_api_root(api_root, upload_ids, dry_run, stats)

    result = {"version": RETIREMENT_VERSION, "status": "dry_run" if dry_run else "applied", "marker": str(marker), **stats}
    if not dry_run:
        _write_json(marker, {"version": RETIREMENT_VERSION, "applied_at": time.time(), "stats": stats})
    return result
