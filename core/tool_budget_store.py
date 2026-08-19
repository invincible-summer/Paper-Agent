"""Runtime per-tool time-budget policy stored in ``data/users.db``.

``agents/tools_impl.py::_TOOL_BUDGETS`` used to be the only source of tool
budgets.  It moves here as ``CODE_TOOL_BUDGETS`` (the code-level defaults) and
an administrator can override every tool individually at
``/admin/performance``; overrides survive restarts.  The project is a
single-Uvicorn-worker deployment, so a short in-process cache is sufficient
and updates apply on the very next tool call without a restart.

Resolution order for one tool's budget: explicit override → code default →
``default_budget_seconds`` (the fallback for tools without a code entry).
``reserve_seconds`` is the turn-level reserve subtracted when a tool budget
would overflow the remaining turn time; it applies to every tool.

Upper bound rationale (surfaced in the admin UI): the 清小搭 gateway times a
whole request out at 120 s, the /v1 channel therefore runs 95 s soft / 105 s
hard turn deadlines, so a budget above 105 s can never take effect and values
above ~``95 - reserve`` only take effect on the web channel (240 s / 300 s).
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "users.db"
_CACHE_TTL_SECONDS = 5.0

BUDGET_MIN_SECONDS = 5.0
BUDGET_MAX_SECONDS = 105.0
RESERVE_MIN_SECONDS = 2.0
RESERVE_MAX_SECONDS = 30.0

# Code-level defaults (previously the hardcoded ``_TOOL_BUDGETS`` dict).
CODE_TOOL_BUDGETS: dict[str, float] = {
    "search_papers": 45.0,
    "research_map": 45.0,
    "deep_read": 75.0,
    "reading_path": 20.0,
    "write_review": 60.0,
    "field_census": 35.0,
    "integrity_sweep": 35.0,
}
CODE_FALLBACK_BUDGET = 30.0
CODE_FALLBACK_RESERVE = 8.0


class ToolBudgetSettingsError(Exception):
    """Safe validation error."""


class ToolBudgetVersionConflict(ToolBudgetSettingsError):
    """Optimistic-lock conflict."""


@dataclass(frozen=True)
class ToolBudgetPolicy:
    budgets: dict[str, float]
    default_budget_seconds: float
    reserve_seconds: float
    version: int = 1
    updated_by: str = "bootstrap"
    updated_at: float = 0.0


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tool_budget_policy (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               budgets_json TEXT NOT NULL,
               default_budget_seconds REAL NOT NULL,
               reserve_seconds REAL NOT NULL,
               version INTEGER NOT NULL,
               updated_by TEXT NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    conn.commit()
    return conn


def _parse_budgets(raw: str) -> dict[str, float]:
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


_cache: tuple[str, float, ToolBudgetPolicy] | None = None


def reset_cache() -> None:
    global _cache
    _cache = None


def _row_to_policy(row) -> ToolBudgetPolicy:
    return ToolBudgetPolicy(
        budgets=_parse_budgets(row[0]),
        default_budget_seconds=float(row[1]),
        reserve_seconds=float(row[2]),
        version=int(row[3]), updated_by=str(row[4] or "bootstrap"),
        updated_at=float(row[5] or 0.0),
    )


def get_tool_budget_policy() -> ToolBudgetPolicy:
    global _cache
    now = time.time()
    if _cache and _cache[0] == str(_DB_PATH) and now - _cache[1] < _CACHE_TTL_SECONDS:
        return _cache[2]
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT budgets_json, default_budget_seconds, reserve_seconds, "
            "version, updated_by, updated_at FROM tool_budget_policy WHERE id = 1"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tool_budget_policy (id, budgets_json, default_budget_seconds, "
                "reserve_seconds, version, updated_by, updated_at) "
                "VALUES (1, '{}', ?, ?, 1, 'bootstrap', ?)",
                (CODE_FALLBACK_BUDGET, CODE_FALLBACK_RESERVE, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT budgets_json, default_budget_seconds, reserve_seconds, "
                "version, updated_by, updated_at FROM tool_budget_policy WHERE id = 1"
            ).fetchone()
    finally:
        conn.close()
    policy = _row_to_policy(row)
    _cache = (str(_DB_PATH), now, policy)
    return policy


def _validate(policy: ToolBudgetPolicy) -> None:
    for name, value in sorted(policy.budgets.items()):
        if not BUDGET_MIN_SECONDS <= value <= BUDGET_MAX_SECONDS:
            raise ToolBudgetSettingsError(
                f"工具 {name} 的预算必须在 {BUDGET_MIN_SECONDS:.0f}–{BUDGET_MAX_SECONDS:.0f} 秒之间")
    if not BUDGET_MIN_SECONDS <= policy.default_budget_seconds <= BUDGET_MAX_SECONDS:
        raise ToolBudgetSettingsError(
            f"默认预算必须在 {BUDGET_MIN_SECONDS:.0f}–{BUDGET_MAX_SECONDS:.0f} 秒之间")
    if not RESERVE_MIN_SECONDS <= policy.reserve_seconds <= RESERVE_MAX_SECONDS:
        raise ToolBudgetSettingsError(
            f"整轮预留量必须在 {RESERVE_MIN_SECONDS:.0f}–{RESERVE_MAX_SECONDS:.0f} 秒之间")


def update_tool_budget_policy(changes: dict, *, expected_version: int,
                              updated_by: str) -> ToolBudgetPolicy:
    allowed = {"budgets", "default_budget_seconds", "reserve_seconds"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise ToolBudgetSettingsError(f"未知设置字段：{', '.join(unknown)}")
    current = get_tool_budget_policy()
    values = asdict(current)
    values.update(changes)
    values["version"] = current.version
    values["updated_by"] = updated_by
    values["updated_at"] = time.time()
    candidate = ToolBudgetPolicy(**values)
    _validate(candidate)

    conn = _connect()
    try:
        cursor = conn.execute(
            """UPDATE tool_budget_policy SET budgets_json = ?,
                   default_budget_seconds = ?, reserve_seconds = ?,
                   version = version + 1, updated_by = ?, updated_at = ?
               WHERE id = 1 AND version = ?""",
            (json.dumps(candidate.budgets, sort_keys=True),
             candidate.default_budget_seconds, candidate.reserve_seconds,
             updated_by, candidate.updated_at, int(expected_version)),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise ToolBudgetVersionConflict("工具时限预算已被其他管理员修改，请刷新后重试")
        conn.commit()
    finally:
        conn.close()
    reset_cache()
    return get_tool_budget_policy()


def resolve_tool_budget(policy: ToolBudgetPolicy, name: str) -> float:
    """Override → code default → default_budget_seconds."""
    if name in policy.budgets:
        return float(policy.budgets[name])
    return float(CODE_TOOL_BUDGETS.get(name, policy.default_budget_seconds))


def get_tool_budget(name: str) -> float:
    """Effective budget for one tool; code defaults on any storage failure."""
    try:
        return resolve_tool_budget(get_tool_budget_policy(), name)
    except Exception:  # pragma: no cover - degraded-storage fallback
        return float(CODE_TOOL_BUDGETS.get(name, CODE_FALLBACK_BUDGET))


def get_turn_reserve() -> float:
    """Turn-level reserve for every tool; code default on storage failure."""
    try:
        return float(get_tool_budget_policy().reserve_seconds)
    except Exception:  # pragma: no cover - degraded-storage fallback
        return CODE_FALLBACK_RESERVE
