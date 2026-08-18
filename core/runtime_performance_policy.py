"""Administrator-controlled runtime performance policy.

The policy is deliberately stored beside the other administrator settings in
``data/users.db``.  This process is deployed as one Uvicorn worker, therefore a
short read cache is sufficient and writes use an optimistic-lock version.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "users.db"
_CACHE_TTL_SECONDS = 5.0

STARTUP_PREWARM_MODES = ("blocking", "background", "role_first", "off")
MAP_CITATION_MODES = ("fast", "quality", "off")


class PerformancePolicyError(ValueError):
    pass


class PerformancePolicyVersionConflict(PerformancePolicyError):
    pass


@dataclass(frozen=True)
class RuntimePerformancePolicy:
    startup_prewarm_mode: Literal["blocking", "background", "role_first", "off"] = "blocking"
    map_citation_mode: Literal["fast", "quality", "off"] = "fast"
    version: int = 1
    updated_by: str = "bootstrap"
    updated_at: float = 0.0


_cache_lock = threading.Lock()
_cache: tuple[float, RuntimePerformancePolicy] | None = None


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runtime_performance_policy (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            startup_prewarm_mode TEXT NOT NULL DEFAULT 'blocking',
            map_citation_mode TEXT NOT NULL DEFAULT 'fast',
            version INTEGER NOT NULL DEFAULT 1,
            updated_by TEXT NOT NULL DEFAULT 'bootstrap',
            updated_at REAL NOT NULL DEFAULT 0
        )"""
    )
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(runtime_performance_policy)")}
    migrations = {
        "startup_prewarm_mode": "TEXT NOT NULL DEFAULT 'blocking'",
        "map_citation_mode": "TEXT NOT NULL DEFAULT 'fast'",
        "version": "INTEGER NOT NULL DEFAULT 1",
        "updated_by": "TEXT NOT NULL DEFAULT 'bootstrap'",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    }
    for column, definition in migrations.items():
        if column not in columns:
            conn.execute(
                f"ALTER TABLE runtime_performance_policy ADD COLUMN {column} {definition}")
    conn.commit()
    return conn


def _seed_from_config() -> RuntimePerformancePolicy:
    # Settings are intentionally only a first-install seed.  Bad config cannot
    # make the application unusable; strict values are enforced at API writes.
    try:
        from core.config import get_settings
        raw = getattr(get_settings(), "performance", None)
        startup = getattr(raw, "startup_prewarm_mode", "blocking")
        citation = getattr(raw, "map_citation_mode", "fast")
    except Exception:
        startup, citation = "blocking", "fast"
    if startup not in STARTUP_PREWARM_MODES:
        startup = "blocking"
    if citation not in MAP_CITATION_MODES:
        citation = "fast"
    return RuntimePerformancePolicy(startup_prewarm_mode=startup, map_citation_mode=citation)


def _row_to_policy(row) -> RuntimePerformancePolicy:
    return RuntimePerformancePolicy(
        startup_prewarm_mode=str(row[0]), map_citation_mode=str(row[1]),
        version=int(row[2]), updated_by=str(row[3]), updated_at=float(row[4]),
    )


def get_performance_policy() -> RuntimePerformancePolicy:
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache and now - _cache[0] < _CACHE_TTL_SECONDS:
            return _cache[1]
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT startup_prewarm_mode,map_citation_mode,version,updated_by,updated_at "
            "FROM runtime_performance_policy WHERE id=1"
        ).fetchone()
        if row is None:
            seed = _seed_from_config()
            conn.execute(
                "INSERT INTO runtime_performance_policy "
                "(id,startup_prewarm_mode,map_citation_mode,version,updated_by,updated_at) "
                "VALUES (1,?,?,?,?,?)",
                (seed.startup_prewarm_mode, seed.map_citation_mode, 1, seed.updated_by, time.time()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT startup_prewarm_mode,map_citation_mode,version,updated_by,updated_at "
                "FROM runtime_performance_policy WHERE id=1"
            ).fetchone()
        policy = _row_to_policy(row)
    finally:
        conn.close()
    with _cache_lock:
        _cache = (now, policy)
    return policy


def reset_performance_policy_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def update_performance_policy(changes: dict, *, expected_version: int, updated_by: str) -> RuntimePerformancePolicy:
    if not isinstance(expected_version, int) or expected_version <= 0:
        raise PerformancePolicyError("expected_version 必须为正整数")
    if not changes:
        raise PerformancePolicyError("至少提供一个性能策略字段")
    unknown = set(changes) - {"startup_prewarm_mode", "map_citation_mode"}
    if unknown:
        raise PerformancePolicyError(f"未知性能策略字段：{', '.join(sorted(unknown))}")
    for key, value in changes.items():
        values = STARTUP_PREWARM_MODES if key == "startup_prewarm_mode" else MAP_CITATION_MODES
        if not isinstance(value, str) or value not in values:
            raise PerformancePolicyError(f"{key} 必须为：{', '.join(values)}")
    if not isinstance(updated_by, str) or not updated_by.strip():
        raise PerformancePolicyError("updated_by 不能为空")
    # Ensure the singleton exists before taking the update connection.
    get_performance_policy()
    conn = _connect()
    try:
        current = conn.execute("SELECT version FROM runtime_performance_policy WHERE id=1").fetchone()
        fields = ", ".join(f"{k} = ?" for k in sorted(changes))
        values = [changes[k] for k in sorted(changes)]
        values.extend([updated_by[:128], time.time(), expected_version])
        cur = conn.execute(
            f"UPDATE runtime_performance_policy SET {fields}, updated_by=?, updated_at=?, version=version+1 "
            "WHERE id=1 AND version=?", values)
        if cur.rowcount != 1:
            conn.rollback()
            raise PerformancePolicyVersionConflict("性能策略已被其他管理员修改，请刷新后重试")
        conn.commit()
    finally:
        conn.close()
    reset_performance_policy_cache()
    return get_performance_policy()


def policy_dict(policy: RuntimePerformancePolicy | None = None) -> dict:
    return asdict(policy or get_performance_policy())

# Descriptive aliases used by integrations and focused tests.
get_runtime_performance_policy = get_performance_policy
update_runtime_performance_policy = update_performance_policy
reset_cache = reset_performance_policy_cache
