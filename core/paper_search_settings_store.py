"""Runtime paper-platform capability and remote-fulltext policy in ``data/users.db``.

The single policy row is the authoritative administrator configuration.  Each
registered source has independent persistent ``search``, ``abstract`` and
``fulltext`` capability state.  Diagnostics may close a capability, but a
successful re-check never re-enables it; only an administrator can do that.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable

from tools.search.registry import SOURCE_IDS, SOURCE_SPECS, source_defaults

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "users.db"
_CACHE_TTL_SECONDS = 5.0

FETCH_MODES = ("enabled", "explicit_only", "probe_only", "disabled")
DISCLOSURE_MODES = ("affected_only", "silent")
ROUTING_MODES = ("smart", "all_enabled")
PAPER_CAPABILITIES = ("search", "abstract", "fulltext")
AUXILIARY_SOURCE_IDS = ("unpaywall", "doi")
CAPABILITY_SOURCE_IDS = SOURCE_IDS + AUXILIARY_SOURCE_IDS
_AUXILIARY_DISPLAY_NAMES = {"unpaywall": "Unpaywall", "doi": "doi.org"}
DIAGNOSTIC_STATUSES = ("ok", "slow", "failed", "not_applicable", "not_configured", "empty", None)


class PaperSearchSettingsError(Exception):
    """Safe validation error."""


class PaperSearchVersionConflict(PaperSearchSettingsError):
    """Optimistic-lock conflict."""


def _empty_capability(enabled: bool) -> dict:
    return {
        "enabled": bool(enabled), "disabled_by": None, "reason_code": None,
        "reason": None, "disabled_at": None, "last_checked_at": None,
        "last_diagnostic_status": None, "last_latency_ms": None,
        "last_kb_per_second": None,
    }


def _supports(source: str, capability: str) -> bool:
    if source == "unpaywall":
        return capability == "fulltext"
    if source == "doi":
        return False
    spec = SOURCE_SPECS[source]
    if capability == "search":
        return True
    if capability == "abstract":
        return bool(getattr(spec, "supports_abstract", True))
    return bool(getattr(spec, "supports_fulltext", spec.supports_pdf_probe))


def _display_name(source: str) -> str:
    if source in SOURCE_SPECS:
        return SOURCE_SPECS[source].display_name
    return _AUXILIARY_DISPLAY_NAMES.get(source, source)


def source_capability_supported(source: str, capability: str) -> bool:
    if source not in CAPABILITY_SOURCE_IDS:
        raise PaperSearchSettingsError(f"未知论文渠道：{source}")
    if capability not in PAPER_CAPABILITIES:
        raise PaperSearchSettingsError(f"未知平台能力：{capability}")
    return _supports(source, capability)


def capability_defaults(sources: dict[str, bool] | None = None, *, existing_install: bool = False) -> dict[str, dict[str, dict]]:
    source_flags = sources or source_defaults(existing_install=existing_install)
    return {
        source: {
            capability: _empty_capability(
                bool(source_flags.get(source, False)) if capability == "search" else _supports(source, capability)
            )
            for capability in PAPER_CAPABILITIES
        }
        for source in CAPABILITY_SOURCE_IDS
    }


def _normalize_capability_record(value: dict | None, *, enabled_default: bool) -> dict:
    row = _empty_capability(enabled_default)
    if isinstance(value, dict):
        for key in row:
            if key in value:
                row[key] = value[key]
    row["enabled"] = bool(row["enabled"])
    if row["disabled_by"] not in {None, "administrator", "diagnostic"}:
        row["disabled_by"] = None
    if row["last_diagnostic_status"] not in DIAGNOSTIC_STATUSES:
        row["last_diagnostic_status"] = None
    for key in ("disabled_at", "last_checked_at", "last_latency_ms", "last_kb_per_second"):
        if row[key] is not None:
            try:
                row[key] = float(row[key])
            except (TypeError, ValueError):
                row[key] = None
    for key in ("reason_code", "reason"):
        row[key] = str(row[key])[:500] if row[key] else None
    return row


def _normalize_capabilities(value: dict | None, sources: dict[str, bool]) -> dict[str, dict[str, dict]]:
    raw = value if isinstance(value, dict) else {}
    result: dict[str, dict[str, dict]] = {}
    for source in CAPABILITY_SOURCE_IDS:
        source_raw = raw.get(source) if isinstance(raw.get(source), dict) else {}
        result[source] = {}
        for capability in PAPER_CAPABILITIES:
            default_enabled = (
                bool(sources.get(source, False))
                if capability == "search" and source in SOURCE_IDS
                else _supports(source, capability)
            )
            result[source][capability] = _normalize_capability_record(
                source_raw.get(capability), enabled_default=default_enabled)
        # ``sources`` was the old authoritative search switch.  During migration
        # preserve it exactly, including disabled sources.
        if source in SOURCE_IDS and (source not in raw or "search" not in source_raw):
            result[source]["search"]["enabled"] = bool(sources.get(source, False))
    return result


@dataclass(frozen=True)
class PaperSearchPolicy:
    sources: dict[str, bool]
    capabilities: dict[str, dict[str, dict]] = field(default_factory=dict)
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
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_search_policy (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               sources_json TEXT NOT NULL,
               capabilities_json TEXT NOT NULL DEFAULT '{}',
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
        conn.execute("ALTER TABLE paper_search_policy ADD COLUMN force_fulltext_probe INTEGER NOT NULL DEFAULT 1")
    if "capabilities_json" not in columns:
        conn.execute("ALTER TABLE paper_search_policy ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '{}'")
    conn.execute("UPDATE paper_search_policy SET search_deadline_seconds=30 WHERE search_deadline_seconds>30")
    conn.execute("UPDATE paper_search_policy SET fulltext_verify_timeout_seconds=30 WHERE fulltext_verify_timeout_seconds>30")

    row = conn.execute("SELECT sources_json, capabilities_json FROM paper_search_policy WHERE id=1").fetchone()
    if row:
        try:
            old_sources = json.loads(row[0])
        except Exception:
            old_sources = {}
        sources = source_defaults(existing_install=True)
        sources.update({k: bool(v) for k, v in old_sources.items() if k in SOURCE_IDS})
        try:
            old_capabilities = json.loads(row[1])
        except Exception:
            old_capabilities = {}
        capabilities = _normalize_capabilities(old_capabilities, sources)
        # Keep compatibility projection synchronized with search capability.
        sources = {name: capabilities[name]["search"]["enabled"] for name in SOURCE_IDS}
        conn.execute(
            "UPDATE paper_search_policy SET sources_json=?, capabilities_json=? WHERE id=1",
            (json.dumps(sources, sort_keys=True), json.dumps(capabilities, ensure_ascii=False, sort_keys=True)),
        )
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_search_diagnostic_runs (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, started_at REAL NOT NULL,
        finished_at REAL NOT NULL, summary_json TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_search_diagnostic_items (
        run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, payload_json TEXT NOT NULL,
        PRIMARY KEY (run_id, ordinal),
        FOREIGN KEY (run_id) REFERENCES paper_search_diagnostic_runs(id) ON DELETE CASCADE)""")
    # Split connectivity/download diagnostics were removed in favour of the
    # capability matrix.  Drop their legacy rows during the idempotent schema
    # migration so old installations do not keep exposing obsolete state.
    conn.execute(
        "DELETE FROM paper_search_diagnostic_runs WHERE kind <> 'capability'"
    )
    conn.commit()
    return conn


def _seed() -> PaperSearchPolicy:
    try:
        from core.config import get_settings
        search = get_settings().search
        defaults = source_defaults()
        raw_sources = getattr(search, "sources", {}) or {}
        sources = {name: bool(raw_sources.get(name, defaults[name])) for name in SOURCE_IDS}
        return PaperSearchPolicy(
            sources=sources, capabilities=capability_defaults(sources),
            search_deadline_seconds=int(getattr(search, "search_deadline_seconds", 30)),
            per_source_timeout_seconds=int(getattr(search, "per_source_timeout_seconds", 12)),
            verify_fulltext=bool(getattr(search, "verify_fulltext", True)),
            fulltext_verify_timeout_seconds=int(getattr(search, "fulltext_verify_timeout_seconds", 30)),
            force_fulltext_probe=bool(getattr(search, "force_fulltext_probe", True)),
        )
    except Exception:
        sources = source_defaults()
        return PaperSearchPolicy(sources=sources, capabilities=capability_defaults(sources))


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
        sources = _normalize_sources(json.loads(row[0]))
    except Exception:
        sources = _normalize_sources({})
    try:
        capabilities = _normalize_capabilities(json.loads(row[1]), sources)
    except Exception:
        capabilities = _normalize_capabilities({}, sources)
    sources = {name: capabilities[name]["search"]["enabled"] for name in SOURCE_IDS}
    return PaperSearchPolicy(
        sources=sources, capabilities=capabilities,
        search_deadline_seconds=int(row[2]), per_source_timeout_seconds=int(row[3]),
        verify_fulltext=bool(row[4]), fulltext_verify_timeout_seconds=int(row[5]),
        paper_fetch_mode=str(row[6]) if row[6] in FETCH_MODES else "enabled",
        fetch_policy_disclosure=str(row[7]) if row[7] in DISCLOSURE_MODES else "affected_only",
        routing_mode=str(row[8]) if row[8] in ROUTING_MODES else "smart",
        version=int(row[9]), updated_by=str(row[10] or "bootstrap"), updated_at=float(row[11] or 0),
        force_fulltext_probe=bool(row[12]),
    )


def get_paper_search_policy() -> PaperSearchPolicy:
    global _cache
    now = time.time()
    if _cache and _cache[0] == str(_DB_PATH) and now - _cache[1] < _CACHE_TTL_SECONDS:
        return _cache[2]
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT sources_json, capabilities_json, search_deadline_seconds, per_source_timeout_seconds, "
            "verify_fulltext, fulltext_verify_timeout_seconds, paper_fetch_mode, fetch_policy_disclosure, "
            "routing_mode, version, updated_by, updated_at, force_fulltext_probe FROM paper_search_policy WHERE id=1"
        ).fetchone()
        if row is None:
            seed = _seed()
            capabilities = _normalize_capabilities(seed.capabilities, seed.sources)
            conn.execute(
                """INSERT INTO paper_search_policy
                (id,sources_json,capabilities_json,search_deadline_seconds,per_source_timeout_seconds,
                 verify_fulltext,fulltext_verify_timeout_seconds,paper_fetch_mode,fetch_policy_disclosure,
                 routing_mode,force_fulltext_probe,version,updated_by,updated_at)
                VALUES(1,?,?,?,?,?,?,?,?,?,?,1,'bootstrap',?)""",
                (json.dumps(seed.sources, sort_keys=True), json.dumps(capabilities, ensure_ascii=False, sort_keys=True),
                 seed.search_deadline_seconds, seed.per_source_timeout_seconds, int(seed.verify_fulltext),
                 seed.fulltext_verify_timeout_seconds, seed.paper_fetch_mode, seed.fetch_policy_disclosure,
                 seed.routing_mode, int(seed.force_fulltext_probe), time.time()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT sources_json, capabilities_json, search_deadline_seconds, per_source_timeout_seconds, "
                "verify_fulltext, fulltext_verify_timeout_seconds, paper_fetch_mode, fetch_policy_disclosure, "
                "routing_mode, version, updated_by, updated_at, force_fulltext_probe FROM paper_search_policy WHERE id=1"
            ).fetchone()
        policy = _row_to_policy(row)
    finally:
        conn.close()
    _cache = (str(_DB_PATH), now, policy)
    return policy


def _validate_policy(candidate: PaperSearchPolicy) -> None:
    if not 10 <= candidate.search_deadline_seconds <= 30:
        raise PaperSearchSettingsError("检索总时限必须为 10–30 秒")
    if not 3 <= candidate.per_source_timeout_seconds <= 30:
        raise PaperSearchSettingsError("单渠道时限必须为 3–30 秒")
    if candidate.per_source_timeout_seconds > candidate.search_deadline_seconds:
        raise PaperSearchSettingsError("单渠道时限不能大于检索总时限")
    if not 0 <= candidate.fulltext_verify_timeout_seconds <= 30:
        raise PaperSearchSettingsError("全文验证时限必须为 0–30 秒")
    if candidate.paper_fetch_mode not in FETCH_MODES or candidate.fetch_policy_disclosure not in DISCLOSURE_MODES or candidate.routing_mode not in ROUTING_MODES:
        raise PaperSearchSettingsError("论文检索策略枚举值无效")


def update_paper_search_policy(changes: dict, *, expected_version: int, updated_by: str) -> PaperSearchPolicy:
    current = get_paper_search_policy()
    allowed = {"sources", "search_deadline_seconds", "per_source_timeout_seconds", "verify_fulltext",
               "fulltext_verify_timeout_seconds", "force_fulltext_probe", "paper_fetch_mode",
               "fetch_policy_disclosure", "routing_mode"}
    unknown = set(changes) - allowed
    if unknown:
        raise PaperSearchSettingsError(f"未知设置字段：{', '.join(sorted(unknown))}")
    values = asdict(current)
    capabilities = _normalize_capabilities(current.capabilities, current.sources)
    if "sources" in changes:
        values["sources"] = _normalize_sources(changes["sources"], base=current.sources)
        now = time.time()
        for source, enabled in values["sources"].items():
            if enabled == current.sources[source]:
                continue
            row = capabilities[source]["search"]
            row["enabled"] = enabled
            row["disabled_by"] = None if enabled else "administrator"
            row["reason_code"] = None if enabled else "administrator_disabled"
            row["reason"] = None if enabled else "管理员已关闭论文搜索能力。"
            row["disabled_at"] = None if enabled else now
    for key, value in changes.items():
        if key != "sources":
            values[key] = value
    values["capabilities"] = capabilities
    values["updated_by"] = updated_by
    values["updated_at"] = time.time()
    candidate = PaperSearchPolicy(**values)
    _validate_policy(candidate)
    _write_policy(candidate, expected_version)
    return get_paper_search_policy()


def _write_policy(candidate: PaperSearchPolicy, expected_version: int, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or _connect()
    try:
        cursor = conn.execute(
            """UPDATE paper_search_policy SET sources_json=?, capabilities_json=?,
            search_deadline_seconds=?, per_source_timeout_seconds=?, verify_fulltext=?,
            fulltext_verify_timeout_seconds=?, force_fulltext_probe=?, paper_fetch_mode=?,
            fetch_policy_disclosure=?, routing_mode=?, version=version+1, updated_by=?, updated_at=?
            WHERE id=1 AND version=?""",
            (json.dumps(candidate.sources, sort_keys=True), json.dumps(candidate.capabilities, ensure_ascii=False, sort_keys=True),
             candidate.search_deadline_seconds, candidate.per_source_timeout_seconds, int(candidate.verify_fulltext),
             candidate.fulltext_verify_timeout_seconds, int(candidate.force_fulltext_probe), candidate.paper_fetch_mode,
             candidate.fetch_policy_disclosure, candidate.routing_mode, candidate.updated_by, candidate.updated_at,
             int(expected_version)),
        )
        if cursor.rowcount != 1:
            if own:
                conn.rollback()
            raise PaperSearchVersionConflict("论文检索设置已被其他管理员修改，请刷新后重试")
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()
    reset_cache()


def source_capability_status(source: str, capability: str, policy: PaperSearchPolicy | None = None) -> dict:
    if source not in CAPABILITY_SOURCE_IDS:
        raise PaperSearchSettingsError(f"未知论文渠道：{source}")
    if capability not in PAPER_CAPABILITIES:
        raise PaperSearchSettingsError(f"未知平台能力：{capability}")
    policy = policy or get_paper_search_policy()
    return dict(_normalize_capabilities(policy.capabilities, policy.sources)[source][capability])


def source_capability_enabled(source: str | None, capability: str, policy: PaperSearchPolicy | None = None) -> tuple[bool, str | None]:
    if capability not in PAPER_CAPABILITIES:
        raise PaperSearchSettingsError(f"未知平台能力：{capability}")
    policy = policy or get_paper_search_policy()
    names = [part.strip() for part in (source or "").split(",") if part.strip()]
    known = [name for name in names if name in CAPABILITY_SOURCE_IDS]
    if not known:
        return True, None
    for name in known:
        row = source_capability_status(name, capability, policy)
        if not row["enabled"]:
            return False, row.get("reason") or f"{_display_name(name)} 的{capability}能力已关闭"
    return True, None


def set_source_capability(source: str, capability: str, enabled: bool, *, expected_version: int, updated_by: str) -> PaperSearchPolicy:
    current = get_paper_search_policy()
    if current.version != expected_version:
        raise PaperSearchVersionConflict("论文检索设置已被其他管理员修改，请刷新后重试")
    if source not in CAPABILITY_SOURCE_IDS or capability not in PAPER_CAPABILITIES:
        raise PaperSearchSettingsError("未知论文平台或能力")
    if not _supports(source, capability):
        raise PaperSearchSettingsError(f"{_display_name(source)} 不支持该能力")
    capabilities = _normalize_capabilities(current.capabilities, current.sources)
    row = capabilities[source][capability]
    row["enabled"] = enabled
    row["disabled_by"] = None if enabled else "administrator"
    row["reason_code"] = None if enabled else "administrator_disabled"
    labels = {"search": "论文搜索", "abstract": "摘要获取", "fulltext": "全文获取"}
    row["reason"] = (
        None if enabled
        else f"管理员已关闭 {_display_name(source)} 的{labels[capability]}能力。"
    )
    row["disabled_at"] = None if enabled else time.time()
    sources = dict(current.sources)
    if capability == "search" and source in SOURCE_IDS:
        sources[source] = enabled
    candidate = replace(current, sources=sources, capabilities=capabilities, updated_by=updated_by, updated_at=time.time())
    _write_policy(candidate, expected_version)
    return get_paper_search_policy()


_DIAGNOSTIC_ITEM_FIELDS = {
    "source", "target", "capability", "status", "latency_ms", "elapsed_ms",
    "http_status", "request_count", "redirect_count", "redirects",
    "final_domain", "domain", "result_count", "valid_abstract_count",
    "abstract_length", "sample_id", "bytes_read", "kb_per_second",
    "pdf_magic_valid", "exit_code", "error_code", "message",
    "auto_disable", "connectivity_failed",
}


def _diagnostic_scalar(value, *, limit: int = 500):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:limit]


def _sanitize_diagnostic_item(item: dict) -> dict:
    return {
        key: _diagnostic_scalar(value)
        for key, value in item.items()
        if key in _DIAGNOSTIC_ITEM_FIELDS
    }


def _sanitize_platform(platform: dict) -> dict:
    safe = {"source": _diagnostic_scalar(platform.get("source"), limit=80)}
    for key in ("connectivity", "search", "abstract", "fulltext"):
        value = platform.get(key)
        safe[key] = _sanitize_diagnostic_item(value) if isinstance(value, dict) else {}
    auto = platform.get("auto_disabled_capabilities")
    safe["auto_disabled_capabilities"] = [
        str(value)[:32] for value in auto or [] if value in PAPER_CAPABILITIES
    ]
    return safe


def _sanitize_diagnostic_summary(summary: dict) -> dict:
    statuses = summary.get("statuses") if isinstance(summary, dict) else None
    safe_statuses = {}
    if isinstance(statuses, dict):
        for key, value in statuses.items():
            try:
                safe_statuses[str(key)[:40]] = max(0, int(value))
            except (TypeError, ValueError):
                continue
    safe = {
        "count": max(0, int(summary.get("count") or 0)),
        "statuses": safe_statuses,
    }
    auto = summary.get("auto_disabled_capabilities")
    if isinstance(auto, list):
        safe["auto_disabled_capabilities"] = [
            {
                "source": str(row.get("source") or "")[:80],
                "capability": str(row.get("capability") or "")[:32],
            }
            for row in auto if isinstance(row, dict)
            and row.get("capability") in PAPER_CAPABILITIES
        ]
    scope = summary.get("diagnostic_scope")
    if scope in {"single_capability", "complete_platforms", "complete_all"}:
        safe["diagnostic_scope"] = scope
    requested_sources = summary.get("requested_sources")
    if isinstance(requested_sources, list):
        safe["requested_sources"] = [
            str(source)[:80] for source in requested_sources
            if source in CAPABILITY_SOURCE_IDS
        ]
    platforms = summary.get("platforms")
    if isinstance(platforms, list):
        safe["platforms"] = [
            _sanitize_platform(row) for row in platforms if isinstance(row, dict)
        ]
    return safe


def apply_diagnostic_results(
    items: Iterable[dict], *, started_at: float, finished_at: float,
    kind: str = "capability", updated_by: str = "diagnostic",
    platforms: Iterable[dict] | None = None,
    summary_metadata: dict | None = None,
) -> DiagnosticRun:
    """Persist sanitized results and capability closures in one transaction."""
    if kind != "capability":
        raise PaperSearchSettingsError("论文平台诊断类型仅支持 capability")
    safe_items = [_sanitize_diagnostic_item(dict(item)) for item in items]
    run_id = uuid.uuid4().hex
    statuses: dict[str, int] = {}
    for item in safe_items:
        status = str(item.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    summary = {"count": len(safe_items), "statuses": statuses}
    if isinstance(summary_metadata, dict):
        summary.update(_sanitize_diagnostic_summary({
            "count": len(safe_items), "statuses": statuses, **summary_metadata,
        }))
    if platforms is not None:
        summary["platforms"] = [_sanitize_platform(dict(platform)) for platform in platforms]
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT sources_json, capabilities_json, search_deadline_seconds, per_source_timeout_seconds, "
            "verify_fulltext, fulltext_verify_timeout_seconds, paper_fetch_mode, fetch_policy_disclosure, "
            "routing_mode, version, updated_by, updated_at, force_fulltext_probe FROM paper_search_policy WHERE id=1"
        ).fetchone()
        if row is None:
            conn.rollback()
            get_paper_search_policy()
            return apply_diagnostic_results(safe_items, started_at=started_at, finished_at=finished_at,
                                            kind=kind, updated_by=updated_by, platforms=platforms,
                                            summary_metadata=summary_metadata)
        policy = _row_to_policy(row)
        capabilities = _normalize_capabilities(policy.capabilities, policy.sources)
        auto_disabled: list[dict] = []
        for item in safe_items:
            source = item.get("source") or item.get("target")
            capability = item.get("capability")
            if (source not in CAPABILITY_SOURCE_IDS
                    or capability not in PAPER_CAPABILITIES
                    or not _supports(source, capability)):
                continue
            cap = capabilities[source][capability]
            cap["last_checked_at"] = finished_at
            cap["last_diagnostic_status"] = item.get("status")
            cap["last_latency_ms"] = item.get("latency_ms", item.get("elapsed_ms"))
            if capability == "fulltext":
                cap["last_kb_per_second"] = item.get("kb_per_second")
            if item.get("auto_disable"):
                newly_disabled = bool(cap["enabled"])
                if cap.get("disabled_by") != "administrator":
                    cap["enabled"] = False
                    cap["disabled_by"] = "diagnostic"
                    cap["reason_code"] = (
                        item.get("error_code") or "diagnostic_failed"
                    )
                    cap["reason"] = (
                        item.get("message")
                        or "管理员诊断发现该能力不可用，已自动关闭。"
                    )
                    if cap.get("disabled_at") is None:
                        cap["disabled_at"] = finished_at
                    if newly_disabled:
                        auto_disabled.append({
                            "source": source, "capability": capability,
                        })
        sources = {name: capabilities[name]["search"]["enabled"] for name in SOURCE_IDS}
        candidate = replace(policy, sources=sources, capabilities=capabilities, updated_by=updated_by, updated_at=finished_at)
        _write_policy(candidate, policy.version, conn=conn)
        summary["auto_disabled_capabilities"] = auto_disabled
        conn.execute("INSERT INTO paper_search_diagnostic_runs VALUES(?,?,?,?,?)",
                     (run_id, kind, started_at, finished_at, json.dumps(summary, ensure_ascii=False, separators=(",", ":"))))
        conn.executemany("INSERT INTO paper_search_diagnostic_items VALUES(?,?,?)", [
            (run_id, i, json.dumps(item, ensure_ascii=False, separators=(",", ":"))) for i, item in enumerate(safe_items)
        ])
        old = conn.execute("SELECT id FROM paper_search_diagnostic_runs ORDER BY finished_at DESC LIMIT -1 OFFSET 20").fetchall()
        for (old_id,) in old:
            conn.execute("DELETE FROM paper_search_diagnostic_runs WHERE id=?", (old_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    reset_cache()
    return DiagnosticRun(run_id, kind, started_at, finished_at, summary, safe_items)


def save_diagnostic_run(kind: str, started_at: float, finished_at: float, summary: dict, items: Iterable[dict]) -> DiagnosticRun:
    """Persist a sanitized unified capability-matrix diagnostic.

    Production diagnostics use :func:`apply_diagnostic_results` so capability
    closure and result persistence are atomic.  This narrower helper remains
    useful for storage tests/imports, but deliberately rejects the removed
    split ``connectivity`` and ``download`` run kinds.
    """
    if kind != "capability":
        raise PaperSearchSettingsError("论文平台诊断类型仅支持 capability")
    run_id = uuid.uuid4().hex
    safe_items = [_sanitize_diagnostic_item(dict(item)) for item in items]
    summary = _sanitize_diagnostic_summary(dict(summary))
    conn = _connect()
    try:
        conn.execute("INSERT INTO paper_search_diagnostic_runs VALUES(?,?,?,?,?)",
                     (run_id, kind, started_at, finished_at, json.dumps(summary, ensure_ascii=False, separators=(",", ":"))))
        conn.executemany("INSERT INTO paper_search_diagnostic_items VALUES(?,?,?)", [
            (run_id, i, json.dumps(item, ensure_ascii=False, separators=(",", ":"))) for i, item in enumerate(safe_items)
        ])
        old = conn.execute("SELECT id FROM paper_search_diagnostic_runs ORDER BY finished_at DESC LIMIT -1 OFFSET 20").fetchall()
        for (old_id,) in old:
            conn.execute("DELETE FROM paper_search_diagnostic_runs WHERE id=?", (old_id,))
        conn.commit()
    finally:
        conn.close()
    return DiagnosticRun(run_id, kind, started_at, finished_at, summary, safe_items)


def get_latest_diagnostics() -> dict:
    conn = _connect()
    try:
        out: dict[str, object] = {"capability": None}
        row = conn.execute(
            "SELECT id,started_at,finished_at,summary_json "
            "FROM paper_search_diagnostic_runs WHERE kind='capability' "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        if row:
            items = conn.execute(
                "SELECT payload_json FROM paper_search_diagnostic_items "
                "WHERE run_id=? ORDER BY ordinal", (row[0],),
            ).fetchall()
            summary = json.loads(row[3])
            latest = {
                "id": row[0], "kind": "capability",
                "started_at": row[1], "finished_at": row[2],
                "summary": summary,
                "items": [json.loads(item[0]) for item in items],
            }
            if isinstance(summary.get("platforms"), list):
                latest["platforms"] = summary["platforms"]
            out["capability"] = latest
        rows = conn.execute(
            "SELECT id,kind,started_at,finished_at,summary_json "
            "FROM paper_search_diagnostic_runs WHERE kind='capability' "
            "ORDER BY finished_at DESC LIMIT 20"
        ).fetchall()
        out["recent"] = [
            {"id": r[0], "kind": r[1], "started_at": r[2],
             "finished_at": r[3], "summary": json.loads(r[4])}
            for r in rows
        ]
        complete = conn.execute(
            "SELECT finished_at,summary_json FROM paper_search_diagnostic_runs "
            "WHERE kind='capability' ORDER BY finished_at DESC"
        ).fetchall()
        out["last_complete_at"] = next((
            float(row[0]) for row in complete
            if json.loads(row[1]).get("diagnostic_scope")
            in {"complete_platforms", "complete_all"}
        ), None)
        return out
    finally:
        conn.close()


def source_enabled(source: str | None, policy: PaperSearchPolicy | None = None) -> bool:
    return source_capability_enabled(source, "search", policy)[0]


def remote_probe_allowed(policy: PaperSearchPolicy | None = None) -> bool:
    return (policy or get_paper_search_policy()).paper_fetch_mode != "disabled"


def remote_download_allowed(origin: str = "automatic", policy: PaperSearchPolicy | None = None) -> bool:
    mode = (policy or get_paper_search_policy()).paper_fetch_mode
    return mode == "enabled" or (mode == "explicit_only" and origin == "explicit")


def disclose_fetch_policy(policy: PaperSearchPolicy | None = None) -> bool:
    return (policy or get_paper_search_policy()).fetch_policy_disclosure == "affected_only"


def paper_capability_source(paper, capability: str) -> str:
    """Return the evidence-producing source after cross-source dedup."""
    if capability == "abstract":
        return getattr(paper, "abstract_source", "") or getattr(paper, "source", "")
    if capability == "fulltext" and getattr(paper, "pdf_url", None):
        return getattr(paper, "pdf_source", "") or getattr(paper, "source", "")
    return getattr(paper, "source", "")


def paper_abstract_text(paper, policy: PaperSearchPolicy | None = None) -> str:
    """Return abstract only when every attached registered source allows it."""
    source = paper_capability_source(paper, "abstract")
    allowed, reason = source_capability_enabled(source, "abstract", policy)
    if not allowed:
        try:
            paper.abstract_policy_status = "disabled"
            paper.abstract_policy_reason = reason or "source_capability_disabled"
        except Exception:
            pass
        return ""
    try:
        paper.abstract_policy_status = "available"
        paper.abstract_policy_reason = ""
    except Exception:
        pass
    return getattr(paper, "abstract", "") or ""


def capability_skip_result(source: str, capability: str, reason: str | None = None) -> dict:
    display = _display_name(source)
    labels = {"search": "论文搜索", "abstract": "摘要获取", "fulltext": "全文获取"}
    return {"source": source, "capability": capability, "status": "skipped",
            "reason_code": "source_capability_disabled",
            "reason": reason or f"{display} 的{labels.get(capability, capability)}能力已关闭，已跳过远程访问。"}
