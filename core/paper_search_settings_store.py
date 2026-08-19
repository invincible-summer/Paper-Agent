"""Runtime paper-search and remote-fulltext policy stored in ``data/users.db``.

``config/settings.yaml`` seeds the single policy row on first use.  Afterwards
administrator writes are authoritative and survive restarts.  The project is a
single-Uvicorn-worker deployment, so a short in-process cache is sufficient.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from tools.search.registry import SOURCE_IDS, source_defaults

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "users.db"
_CACHE_TTL_SECONDS = 5.0

FETCH_MODES = ("enabled", "explicit_only", "probe_only", "disabled")
DISCLOSURE_MODES = ("affected_only", "silent")
ROUTING_MODES = ("smart", "all_enabled")


class PaperSearchSettingsError(Exception):
    """Safe validation error."""


class PaperSearchVersionConflict(PaperSearchSettingsError):
    """Optimistic-lock conflict."""


@dataclass(frozen=True)
class PaperSearchPolicy:
    sources: dict[str, bool]
    search_deadline_seconds: int = 30
    per_source_timeout_seconds: int = 12
    verify_fulltext: bool = True
    fulltext_verify_timeout_seconds: int = 30
    force_fulltext_probe: bool = True
    paper_fetch_mode: str = "enabled"
    fetch_policy_disclosure: str = "affected_only"
    routing_mode: str = "smart"
    version: int = 1
    updated_by: str = "bootstrap"
    updated_at: float = 0.0


@dataclass(frozen=True)
class DiagnosticRun:
    id: str
    kind: str
    started_at: float
    finished_at: float
    summary: dict
    items: list[dict]


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_search_policy (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               sources_json TEXT NOT NULL,
               search_deadline_seconds INTEGER NOT NULL,
               per_source_timeout_seconds INTEGER NOT NULL,
               verify_fulltext INTEGER NOT NULL,
               fulltext_verify_timeout_seconds INTEGER NOT NULL,
               force_fulltext_probe INTEGER NOT NULL DEFAULT 1,
               paper_fetch_mode TEXT NOT NULL,
               fetch_policy_disclosure TEXT NOT NULL,
               routing_mode TEXT NOT NULL DEFAULT 'smart',
               version INTEGER NOT NULL,
               updated_by TEXT NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_search_policy)")}
    if "routing_mode" not in columns:
        conn.execute("ALTER TABLE paper_search_policy ADD COLUMN routing_mode TEXT NOT NULL DEFAULT 'smart'")
    if "force_fulltext_probe" not in columns:
        # Existing installs upgrade to the forced-probe schedule (the probe used
        # to be silently dropped whenever retrieval ate the whole budget).
        conn.execute("ALTER TABLE paper_search_policy ADD COLUMN force_fulltext_probe INTEGER NOT NULL DEFAULT 1")
    # Search itself has an invariant hard cap; older rows could previously hold
    # up to 120 seconds and must not keep five-minute tool turns alive.
    conn.execute("UPDATE paper_search_policy SET search_deadline_seconds = 30 WHERE search_deadline_seconds > 30")
    conn.execute("UPDATE paper_search_policy SET fulltext_verify_timeout_seconds = 30 WHERE fulltext_verify_timeout_seconds > 30")
    # Existing rows predate the five newly introduced sources. Keep those sources
    # disabled until an administrator reviews configuration/license/index state.
    row = conn.execute("SELECT sources_json FROM paper_search_policy WHERE id = 1").fetchone()
    if row:
        try:
            current_sources = json.loads(row[0])
        except Exception:
            current_sources = {}
        merged = source_defaults(existing_install=True)
        merged.update({k: bool(v) for k, v in current_sources.items() if k in SOURCE_IDS})
        if merged != current_sources:
            conn.execute("UPDATE paper_search_policy SET sources_json = ? WHERE id = 1",
                         (json.dumps(merged, sort_keys=True),))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_search_diagnostic_runs (
               id TEXT PRIMARY KEY,
               kind TEXT NOT NULL,
               started_at REAL NOT NULL,
               finished_at REAL NOT NULL,
               summary_json TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_search_diagnostic_items (
               run_id TEXT NOT NULL,
               ordinal INTEGER NOT NULL,
               payload_json TEXT NOT NULL,
               PRIMARY KEY (run_id, ordinal),
               FOREIGN KEY (run_id) REFERENCES paper_search_diagnostic_runs(id)
                   ON DELETE CASCADE
           )"""
    )
    conn.commit()
    return conn


def _seed() -> PaperSearchPolicy:
    try:
        from core.config import get_settings
        search = get_settings().search
        raw_sources = getattr(search, "sources", {}) or {}
        defaults = source_defaults()
        sources = {name: bool(raw_sources.get(name, defaults[name])) for name in SOURCE_IDS}
        return PaperSearchPolicy(
            sources=sources,
            search_deadline_seconds=int(getattr(search, "search_deadline_seconds", 30)),
            per_source_timeout_seconds=int(getattr(search, "per_source_timeout_seconds", 12)),
            verify_fulltext=bool(getattr(search, "verify_fulltext", True)),
            fulltext_verify_timeout_seconds=int(
                getattr(search, "fulltext_verify_timeout_seconds", 30)),
            force_fulltext_probe=bool(getattr(search, "force_fulltext_probe", True)),
        )
    except Exception:  # pragma: no cover - standalone recovery fallback
        return PaperSearchPolicy(sources=source_defaults())


_cache: tuple[str, float, PaperSearchPolicy] | None = None


def reset_cache() -> None:
    global _cache
    _cache = None


def _normalize_sources(value: dict | None, *, base: dict[str, bool] | None = None) -> dict[str, bool]:
    defaults = source_defaults(existing_install=base is not None)
    result = dict(defaults)
    if base is not None:
        result.update(base)
    if value is None:
        return {name: bool(result.get(name, defaults[name])) for name in SOURCE_IDS}
    unknown = sorted(set(value) - set(SOURCE_IDS))
    if unknown:
        raise PaperSearchSettingsError(f"未知论文渠道：{', '.join(unknown)}")
    for name, enabled in value.items():
        if not isinstance(enabled, bool):
            raise PaperSearchSettingsError(f"渠道 {name} 的开关必须是布尔值")
        result[name] = enabled
    return {name: bool(result.get(name, defaults[name])) for name in SOURCE_IDS}


def _row_to_policy(row) -> PaperSearchPolicy:
    try:
        sources_raw = json.loads(row[0])
    except Exception:
        sources_raw = {}
    return PaperSearchPolicy(
        sources=_normalize_sources(sources_raw),
        search_deadline_seconds=int(row[1]),
        per_source_timeout_seconds=int(row[2]),
        verify_fulltext=bool(row[3]),
        fulltext_verify_timeout_seconds=int(row[4]),
        force_fulltext_probe=bool(row[11]),
        paper_fetch_mode=str(row[5]) if row[5] in FETCH_MODES else "enabled",
        fetch_policy_disclosure=str(row[6]) if row[6] in DISCLOSURE_MODES else "affected_only",
        routing_mode=str(row[7]) if row[7] in ROUTING_MODES else "smart",
        version=int(row[8]), updated_by=str(row[9] or "bootstrap"),
        updated_at=float(row[10] or 0.0),
    )


def get_paper_search_policy() -> PaperSearchPolicy:
    global _cache
    now = time.time()
    if _cache and _cache[0] == str(_DB_PATH) and now - _cache[1] < _CACHE_TTL_SECONDS:
        return _cache[2]
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT sources_json, search_deadline_seconds, per_source_timeout_seconds, "
            "verify_fulltext, fulltext_verify_timeout_seconds, paper_fetch_mode, "
            "fetch_policy_disclosure, routing_mode, version, updated_by, updated_at, "
            "force_fulltext_probe "
            "FROM paper_search_policy WHERE id = 1"
        ).fetchone()
        if row is None:
            seed = _seed()
            conn.execute(
                "INSERT INTO paper_search_policy (id, sources_json, search_deadline_seconds, per_source_timeout_seconds, verify_fulltext, fulltext_verify_timeout_seconds, paper_fetch_mode, fetch_policy_disclosure, routing_mode, force_fulltext_probe, version, updated_by, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'bootstrap', ?)",
                (json.dumps(seed.sources, sort_keys=True), seed.search_deadline_seconds,
                 seed.per_source_timeout_seconds, int(seed.verify_fulltext),
                 seed.fulltext_verify_timeout_seconds, seed.paper_fetch_mode,
                 seed.fetch_policy_disclosure, seed.routing_mode,
                 int(seed.force_fulltext_probe), now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT sources_json, search_deadline_seconds, per_source_timeout_seconds, "
                "verify_fulltext, fulltext_verify_timeout_seconds, paper_fetch_mode, "
                "fetch_policy_disclosure, routing_mode, version, updated_by, updated_at, "
                "force_fulltext_probe "
                "FROM paper_search_policy WHERE id = 1"
            ).fetchone()
    finally:
        conn.close()
    policy = _row_to_policy(row)
    _cache = (str(_DB_PATH), now, policy)
    return policy


def _validate(policy: PaperSearchPolicy) -> None:
    if not 10 <= policy.search_deadline_seconds <= 30:
        raise PaperSearchSettingsError("检索总时限必须在 10–30 秒之间")
    if not 3 <= policy.per_source_timeout_seconds <= 30:
        raise PaperSearchSettingsError("单渠道时限必须在 3–30 秒之间")
    if policy.per_source_timeout_seconds > policy.search_deadline_seconds:
        raise PaperSearchSettingsError("单渠道时限不能大于检索总时限")
    if not 0 <= policy.fulltext_verify_timeout_seconds <= 30:
        raise PaperSearchSettingsError("全文探测总时限必须在 0–30 秒之间")
    if policy.paper_fetch_mode not in FETCH_MODES:
        raise PaperSearchSettingsError("未知论文拉取策略")
    if policy.fetch_policy_disclosure not in DISCLOSURE_MODES:
        raise PaperSearchSettingsError("未知用户提示策略")
    if policy.routing_mode not in ROUTING_MODES:
        raise PaperSearchSettingsError("未知论文渠道路由模式")


def update_paper_search_policy(changes: dict, *, expected_version: int, updated_by: str) -> PaperSearchPolicy:
    allowed = {
        "sources", "search_deadline_seconds", "per_source_timeout_seconds",
        "verify_fulltext", "fulltext_verify_timeout_seconds", "force_fulltext_probe",
        "paper_fetch_mode", "fetch_policy_disclosure", "routing_mode",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise PaperSearchSettingsError(f"未知设置字段：{', '.join(unknown)}")
    current = get_paper_search_policy()
    values = asdict(current)
    values.update(changes)
    values["sources"] = _normalize_sources(changes.get("sources"), base=current.sources)
    values["version"] = current.version
    values["updated_by"] = updated_by
    values["updated_at"] = time.time()
    candidate = PaperSearchPolicy(**values)
    _validate(candidate)

    conn = _connect()
    try:
        cursor = conn.execute(
            """UPDATE paper_search_policy SET sources_json = ?,
                   search_deadline_seconds = ?, per_source_timeout_seconds = ?,
                   verify_fulltext = ?, fulltext_verify_timeout_seconds = ?,
                   force_fulltext_probe = ?, paper_fetch_mode = ?, fetch_policy_disclosure = ?,
                   routing_mode = ?,
                   version = version + 1, updated_by = ?, updated_at = ?
               WHERE id = 1 AND version = ?""",
            (json.dumps(candidate.sources, sort_keys=True), candidate.search_deadline_seconds,
             candidate.per_source_timeout_seconds, int(candidate.verify_fulltext),
             candidate.fulltext_verify_timeout_seconds, int(candidate.force_fulltext_probe),
             candidate.paper_fetch_mode,
             candidate.fetch_policy_disclosure, candidate.routing_mode, updated_by, candidate.updated_at,
             int(expected_version)),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise PaperSearchVersionConflict("论文检索设置已被其他管理员修改，请刷新后重试")
        conn.commit()
    finally:
        conn.close()
    reset_cache()
    return get_paper_search_policy()


def save_diagnostic_run(kind: str, started_at: float, finished_at: float,
                        summary: dict, items: Iterable[dict]) -> DiagnosticRun:
    if kind not in {"connectivity", "download"}:
        raise PaperSearchSettingsError("未知诊断类型")
    run_id = uuid.uuid4().hex
    safe_items = [dict(item) for item in items]
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO paper_search_diagnostic_runs VALUES (?, ?, ?, ?, ?)",
            (run_id, kind, float(started_at), float(finished_at),
             json.dumps(summary, ensure_ascii=False, separators=(",", ":"))),
        )
        conn.executemany(
            "INSERT INTO paper_search_diagnostic_items VALUES (?, ?, ?)",
            [(run_id, i, json.dumps(item, ensure_ascii=False, separators=(",", ":")))
             for i, item in enumerate(safe_items)],
        )
        old = conn.execute(
            "SELECT id FROM paper_search_diagnostic_runs ORDER BY finished_at DESC LIMIT -1 OFFSET 20"
        ).fetchall()
        for (old_id,) in old:
            conn.execute("DELETE FROM paper_search_diagnostic_items WHERE run_id = ?", (old_id,))
            conn.execute("DELETE FROM paper_search_diagnostic_runs WHERE id = ?", (old_id,))
        conn.commit()
    finally:
        conn.close()
    return DiagnosticRun(run_id, kind, started_at, finished_at, summary, safe_items)


def get_latest_diagnostics() -> dict:
    conn = _connect()
    try:
        out: dict[str, dict | None] = {"connectivity": None, "download": None}
        for kind in out:
            row = conn.execute(
                "SELECT id, started_at, finished_at, summary_json FROM paper_search_diagnostic_runs "
                "WHERE kind = ? ORDER BY finished_at DESC LIMIT 1", (kind,),
            ).fetchone()
            if row is None:
                continue
            items = conn.execute(
                "SELECT payload_json FROM paper_search_diagnostic_items WHERE run_id = ? ORDER BY ordinal",
                (row[0],),
            ).fetchall()
            out[kind] = {
                "id": row[0], "kind": kind, "started_at": row[1], "finished_at": row[2],
                "summary": json.loads(row[3]), "items": [json.loads(item[0]) for item in items],
            }
        summaries = conn.execute(
            "SELECT id, kind, started_at, finished_at, summary_json "
            "FROM paper_search_diagnostic_runs ORDER BY finished_at DESC LIMIT 20"
        ).fetchall()
        out["recent"] = [
            {"id": r[0], "kind": r[1], "started_at": r[2], "finished_at": r[3],
             "summary": json.loads(r[4])} for r in summaries
        ]
        return out
    finally:
        conn.close()


def source_enabled(source: str | None, policy: PaperSearchPolicy | None = None) -> bool:
    """Whether at least one source attached to a paper is enabled."""
    policy = policy or get_paper_search_policy()
    names = [part.strip() for part in (source or "").split(",") if part.strip()]
    known = [name for name in names if name in SOURCE_IDS]
    return True if not known else all(policy.sources.get(name, False) for name in known)


def remote_probe_allowed(policy: PaperSearchPolicy | None = None) -> bool:
    return (policy or get_paper_search_policy()).paper_fetch_mode != "disabled"


def remote_download_allowed(origin: str = "automatic",
                            policy: PaperSearchPolicy | None = None) -> bool:
    mode = (policy or get_paper_search_policy()).paper_fetch_mode
    if mode == "enabled":
        return True
    if mode == "explicit_only":
        return origin == "explicit"
    return False


def disclose_fetch_policy(policy: PaperSearchPolicy | None = None) -> bool:
    return (policy or get_paper_search_policy()).fetch_policy_disclosure == "affected_only"
