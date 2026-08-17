"""Administrator-only management for long-lived Agent API credentials."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.api.v1.auth import current_user

router = APIRouter(prefix="/admin", tags=["admin"])


class AgentKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _administrator(authorization: str | None) -> dict:
    user = current_user(authorization)
    if user.get("role") != "administrator":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user


@router.get("/agent-keys")
def get_agent_keys(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from core.user_store import list_agent_api_keys

    return {"items": list_agent_api_keys()}


@router.post("/agent-keys", status_code=201)
def post_agent_key(
    body: AgentKeyCreateRequest,
    authorization: str | None = Header(None),
) -> dict:
    administrator = _administrator(authorization)
    from core.user_store import UserStoreError, create_agent_api_key

    try:
        item, raw_key = create_agent_api_key(body.name, administrator["id"])
    except UserStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # The plaintext is deliberately returned only by this creation response.
    return {"item": item, "key": raw_key}


@router.delete("/agent-keys/{key_id}")
def delete_agent_key(
    key_id: str,
    authorization: str | None = Header(None),
) -> dict:
    _administrator(authorization)
    from core.user_store import revoke_agent_api_key

    if not revoke_agent_api_key(key_id):
        raise HTTPException(status_code=404, detail="密钥不存在或已撤销")
    return {"status": "revoked", "id": key_id}

# ---------------------------------------------------------------------------
# OpenAI-compatible API storage administration
# ---------------------------------------------------------------------------

from typing import Literal
from pydantic import ConfigDict, model_validator


class ApiStoragePolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(gt=0)
    preview_token: str | None = None
    preset: Literal["privacy", "balanced", "performance", "custom"] | None = None
    session_ttl_seconds: int | None = Field(default=None, ge=3600, le=365 * 86400)
    upload_ttl_seconds: int | None = Field(default=None, ge=3600, le=365 * 86400)
    export_ttl_seconds: int | None = Field(default=None, ge=3600, le=30 * 86400)
    public_pdf_ttl_seconds: int | None = Field(default=None, ge=3600, le=365 * 86400)
    cache_ttl_seconds: int | None = Field(default=None, ge=3600, le=365 * 86400)
    trace_ttl_seconds: int | None = Field(default=None, ge=3600, le=7 * 86400)
    cleanup_interval_minutes: int | None = Field(default=None, ge=15, le=1440)
    observe_threshold_percent: int | None = Field(default=None, ge=50, le=90)
    pressure_threshold_percent: int | None = Field(default=None, ge=60, le=95)
    critical_threshold_percent: int | None = Field(default=None, ge=70, le=98)
    hard_stop_threshold_percent: Literal[98] | None = None
    pressure_strategy: Literal["continuous_evict"] | None = None
    critical_strategy: Literal["pause_heavy", "emergency_evict"] | None = None
    trace_mode: Literal["off", "metadata", "full"] | None = None

    def changes(self) -> dict:
        return self.model_dump(exclude={"expected_version", "preview_token"}, exclude_none=True)

    @model_validator(mode="after")
    def has_changes(self):
        if not self.changes():
            raise ValueError("至少提供一个策略修改字段")
        return self


class CleanupPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["immediate_cleanup", "policy_update", "legacy_scan", "emergency_evict"]
    proposed: dict = Field(default_factory=dict)


class CleanupExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str = Field(min_length=20, max_length=200)


class AccountCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_type: Literal["web_user", "api_key"]
    account_id: str = Field(min_length=1, max_length=64)


_API_STORAGE_HELP = {
    "version": 2,
    "items": {
        "preset.privacy": {"title": "隐私优先", "does": "会话和上传保留 2 小时，导出 2 小时，公共 PDF 3 天，语义/视觉缓存 30 天，Trace 关闭。", "affected": "API 会话、私有上传、导出、公共论文缓存和模型结果缓存。", "benefits": "磁盘和隐私暴露窗口最小。", "drawbacks": "跨日继续研究能力弱，重复下载、OCR、VLM 和模型费用更高。", "privacy": "最高", "disk": "最低", "latency_cost": "更常重新下载和理解文件", "continuity": "短", "effective": "新 TTL 立即用于后续访问；缩短 TTL 需要预览确认。", "fallback": "98% 保护仍强制生效。", "restore": "选择 balanced。"},
        "preset.balanced": {"title": "均衡（默认）", "does": "会话/上传 7 天、导出 24 小时、公共 PDF 3 天、缓存 90 天、Trace 关闭。", "affected": "全部 API 侧临时研究数据。", "benefits": "兼顾连续性、费用和磁盘。", "drawbacks": "比隐私优先保留更多数据。", "privacy": "中", "disk": "中", "latency_cost": "通常无需频繁重算", "continuity": "7 天", "effective": "保存后立即生效。", "fallback": "磁盘阈值仍优先。", "restore": "点击恢复默认。"},
        "preset.performance": {"title": "性能优先", "does": "会话/上传 30 天、导出 7 天、公共 PDF 3 天、缓存 180 天，metadata Trace 7 天。", "affected": "API 研究状态和可重建缓存。", "benefits": "最低重复下载/OCR/VLM 延迟和费用。", "drawbacks": "磁盘占用和保留窗口最大。", "privacy": "较低", "disk": "高", "latency_cost": "最低", "continuity": "30 天", "effective": "保存后立即生效。", "fallback": "85/95/98 阈值会覆盖保留期。", "restore": "选择 balanced。"},
        "ttl": {"title": "数据保留时间", "does": "分别控制 Checkpoint、上传、导出、公共 PDF、缓存和 Trace 的过期时间。公共 PDF 默认 3 天：过期后由计划清理删除，后续需要时 deep_read 会自动重新下载并提取。", "affected": "仅 /v1 API 数据，不影响自制前端。", "benefits": "可按隐私和复用需求取舍。", "drawbacks": "过短会导致重新下载、OCR、VLM 和上下文中断。", "privacy": "越短越高", "disk": "越短越低", "latency_cost": "越短重复成本越高", "continuity": "由 Session TTL 决定", "effective": "新写入立即采用；清理器处理旧数据。", "fallback": "磁盘压力可提前清理可重建数据。", "restore": "恢复 balanced。"},
        "thresholds": {"title": "75 / 85 / 95 / 98 磁盘阈值", "does": "75% 告警并清过期；85% 连续清理可重建数据；95% 按策略暂停或紧急清理；98% 强制暂停文件重任务。", "affected": "API 上传、下载、深读、OCR、VLM、导出与缓存。", "benefits": "避免磁盘写满导致数据库和服务损坏。", "drawbacks": "压力时可能需要重下文件或重传私有上传。", "privacy": "无额外读取", "disk": "核心保护", "latency_cost": "清理后可能重算", "continuity": "文字问答始终保留", "effective": "下一次守卫/清理立即生效。", "fallback": "98% 不可关闭。", "restore": "75/85/95/98。"},
        "critical.pause_heavy": {"title": "95% 暂停文件重任务（默认）", "does": "暂停上传、下载、deep_read、OCR、VLM 和导出，普通文字问答继续。", "affected": "文件写入型 API 操作。", "benefits": "不提前删除未过期私有上传。", "drawbacks": "需扩容或清理后才能继续深读。", "privacy": "不增加删除", "disk": "停止增长", "latency_cost": "重任务暂不可用", "continuity": "文字聊天继续", "effective": "立即", "fallback": "98% 同样强制暂停。", "restore": "磁盘恢复后自动解除。"},
        "critical.emergency_evict": {"title": "95% 紧急清理", "does": "先删除公共缓存和生成物，再删除 24 小时未访问且非 in-flight 的最旧私有上传。", "affected": "可能影响仍在 TTL 内的 API 文件。", "benefits": "在小磁盘上尽量维持文件服务。", "drawbacks": "私有文件可能要求用户重新上传，公共论文会重下。", "privacy": "更快删除", "disk": "释放最多", "latency_cost": "后续重算成本高", "continuity": "结构化 Checkpoint 尽量保留", "effective": "需预览和二次确认", "fallback": "in-flight/protected 数据不删；98% 仍暂停。", "restore": "切回 pause_heavy。"},
        "trace.off": {"title": "Trace 关闭（默认）", "does": "不保存逐请求 Trace，仅保留匿名请求/错误/Token/耗时聚合。", "affected": "API 可观测性。", "benefits": "隐私和磁盘最佳。", "drawbacks": "排障信息最少。", "privacy": "最高", "disk": "最低", "latency_cost": "无", "continuity": "无影响", "effective": "新请求立即生效", "fallback": "错误聚合仍保留", "restore": "选择 off。"},
        "trace.metadata": {"title": "Metadata Trace", "does": "保存 trace id、模型、工具名、状态、错误码、Token 和耗时，不保存原文、参数、reasoning、文件名或 URL。", "affected": "API 排障元数据，最长 7 天。", "benefits": "低隐私风险的性能/错误诊断。", "drawbacks": "不能复盘具体输入。", "privacy": "高", "disk": "低", "latency_cost": "极低", "continuity": "无影响", "effective": "新请求立即生效", "fallback": "TTL 到期自动删除", "restore": "选择 off。"},
        "trace.full": {"title": "临时 Full Trace", "does": "排障时记录经脱敏的问题和参数；Key、文件 bytes、完整论文正文永不记录，URL query 脱敏。", "affected": "API 调试数据，最长 7 天。", "benefits": "最强问题复盘能力。", "drawbacks": "隐私与磁盘风险最高。", "privacy": "低，需谨慎", "disk": "较高", "latency_cost": "轻微写盘", "continuity": "无影响", "effective": "需预览和二次确认", "fallback": "7 天硬上限", "restore": "排障后立即切回 off。"},
        "cleanup": {"title": "立即清理与遗留扫描", "does": "立即执行过期清理或扫描 API 根目录中的孤立文件/缺失记录。", "affected": "仅 data/openai_api。", "benefits": "快速回收空间、修复中断留下的不一致。", "drawbacks": "删除不可撤销。", "privacy": "不读取用户原文", "disk": "立即释放", "latency_cost": "清理期间有少量 IO", "continuity": "受保护/in-flight 数据跳过", "effective": "预览后二次确认", "fallback": "根目录 containment 和一小时新文件保护", "restore": "不可恢复，必要时重新下载/上传。"},
    },
}


def _api_storage_store():
    from core.api_storage_store import ApiStorageStore
    from core.storage_context import StorageContext
    return ApiStorageStore(StorageContext.openai_api())


@router.get("/api-storage/policy")
def get_api_storage_policy(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from dataclasses import asdict
    store = _api_storage_store(); store.initialize()
    return {"policy": asdict(store.get_policy()), "help": _API_STORAGE_HELP}


@router.put("/api-storage/policy")
def put_api_storage_policy(body: ApiStoragePolicyUpdate,
                           authorization: str | None = Header(None)) -> dict:
    admin = _administrator(authorization)
    from dataclasses import asdict
    from core.api_storage_cleanup import ApiStorageCleanup
    from core.api_storage_store import PolicyVersionConflict
    store = _api_storage_store(); store.initialize()
    current = store.get_policy(); changes = body.changes()
    merged = asdict(current); merged.update(changes)
    thresholds = [merged[k] for k in ("observe_threshold_percent", "pressure_threshold_percent", "critical_threshold_percent", "hard_stop_threshold_percent")]
    if not (0 < thresholds[0] < thresholds[1] < thresholds[2] < thresholds[3] <= 100):
        raise HTTPException(422, "磁盘阈值必须严格满足 observe < pressure < critical < hard")
    dangerous = (
        any(k.endswith("ttl_seconds") and int(v) < int(getattr(current, k)) for k, v in changes.items())
        or changes.get("trace_mode") == "full"
        or changes.get("critical_strategy") == "emergency_evict"
        or any(k.endswith("threshold_percent") and int(v) < int(getattr(current, k)) for k, v in changes.items())
    )
    if dangerous:
        if not body.preview_token:
            raise HTTPException(409, detail={"code": "PREVIEW_REQUIRED", "message": "危险策略修改必须先预览并二次确认"})
        try:
            payload = ApiStorageCleanup(store).consume_preview(
                body.preview_token, action="policy_update", expected_policy_version=body.expected_version
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if payload.get("payload", {}).get("proposed") != changes:
            raise HTTPException(409, "确认内容与预览内容不一致")
    try:
        updated = store.update_policy(changes, expected_version=body.expected_version, updated_by=admin["id"])
    except PolicyVersionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    return {"policy": asdict(updated), "dangerous": dangerous}


@router.get("/api-storage/usage")
def get_api_storage_usage(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    store = _api_storage_store(); store.initialize()
    with store.connect() as conn:
        rows = conn.execute("SELECT category, status, COUNT(*), COALESCE(SUM(size_bytes),0) FROM api_artifacts GROUP BY category,status").fetchall()
    return {"categories": [{"category": r[0], "status": r[1], "count": r[2], "bytes": r[3]} for r in rows], "total_bytes": sum(int(r[3]) for r in rows)}


@router.get("/api-storage/status")
def get_api_storage_status(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from core.storage_pressure import StoragePressureGuard
    store = _api_storage_store(); store.initialize()
    outcome = StoragePressureGuard(store.context).evaluate("upload")
    with store.connect() as conn:
        state = conn.execute("SELECT heavy_writes_paused,pause_reason,disk_percent,last_cleanup_at,updated_at FROM api_runtime_state WHERE id=1").fetchone()
    return {"text_chat_allowed": True, "heavy_writes_paused": bool(state[0]), "pause_reason": state[1], "disk_percent": state[2], "last_cleanup_at": state[3], "updated_at": state[4], "threshold_state": outcome}


@router.get("/api-storage/cleanup-runs")
def get_api_storage_cleanup_runs(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    store = _api_storage_store(); store.initialize()
    with store.connect() as conn:
        rows = conn.execute("SELECT id,mode,started_at,finished_at,status,disk_percent_before,disk_percent_after,reclaimed_bytes,error_code FROM api_cleanup_runs ORDER BY started_at DESC LIMIT 50").fetchall()
    keys = ["id","mode","started_at","finished_at","status","disk_percent_before","disk_percent_after","reclaimed_bytes","error_code"]
    return {"items": [dict(zip(keys, row)) for row in rows]}


@router.post("/api-storage/cleanup/preview")
def post_api_storage_cleanup_preview(body: CleanupPreviewRequest,
                                     authorization: str | None = Header(None)) -> dict:
    admin = _administrator(authorization)
    from core.api_storage_cleanup import ApiStorageCleanup
    store = _api_storage_store(); store.initialize()
    return ApiStorageCleanup(store).create_preview(
        action=body.action, created_by=admin["id"], payload={"proposed": body.proposed}
    )


@router.post("/api-storage/cleanup/execute")
def post_api_storage_cleanup_execute(body: CleanupExecuteRequest,
                                     authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from core.api_storage_cleanup import ApiStorageCleanup
    store = _api_storage_store(); store.initialize()
    try:
        return {"result": ApiStorageCleanup(store).execute_preview(body.token).to_dict()}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/api-storage/legacy-scan")
def post_api_storage_legacy_scan(body: CleanupExecuteRequest,
                                 authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from core.api_storage_cleanup import ApiStorageCleanup
    store = _api_storage_store(); store.initialize(); cleanup = ApiStorageCleanup(store)
    try:
        cleanup.consume_preview(body.token, action="legacy_scan", expected_policy_version=store.get_policy().version)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"result": cleanup.reconcile().to_dict()}


# ---------------------------------------------------------------------------
# Per-account storage census + unrecoverable deletion
# ---------------------------------------------------------------------------

@router.get("/accounts/usage")
def get_accounts_usage(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from core.admin_accounts import list_accounts_data
    return list_accounts_data()


@router.post("/accounts/cleanup")
def post_accounts_cleanup(body: AccountCleanupRequest,
                          authorization: str | None = Header(None)) -> dict:
    admin = _administrator(authorization)
    from core.admin_accounts import delete_api_key_data, delete_web_account_data

    try:
        if body.account_type == "web_user":
            result = delete_web_account_data(body.account_id)
        else:
            result = delete_api_key_data(body.account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail=result.get("reason") or "未找到可清理的数据")
    result["operator"] = admin["id"]
    return result


@router.post("/paper-cache/cleanup")
def post_paper_cache_cleanup(authorization: str | None = Header(None)) -> dict:
    admin = _administrator(authorization)
    from core.admin_accounts import cleanup_web_paper_cache
    return cleanup_web_paper_cache() | {"operator": admin["id"]}


# ---------------------------------------------------------------------------
# OpenAI-compatible /v1 markdown-card display policy (清小搭 卡片展示策略)
# ---------------------------------------------------------------------------


class DisplayPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(gt=0)
    preset: Literal["core", "all", "custom", "off"] | None = None
    enabled_tools: list[str] | None = None
    skill_card_enabled: bool | None = None

    def changes(self) -> dict:
        return self.model_dump(exclude={"expected_version"}, exclude_none=True)

    @model_validator(mode="after")
    def has_changes(self):
        if not self.changes():
            raise ValueError("至少提供一个策略修改字段")
        return self


@router.get("/display-policy")
def get_display_policy(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from dataclasses import asdict

    from tools.export.cards import ALL_CARD_TOOLS, CORE_CARD_TOOLS
    store = _api_storage_store(); store.initialize()
    policy = store.get_display_policy()
    return {
        "policy": asdict(policy),
        "tools": sorted(ALL_CARD_TOOLS),
        "core_tools": sorted(CORE_CARD_TOOLS),
    }


@router.put("/display-policy")
def put_display_policy(body: DisplayPolicyUpdate,
                       authorization: str | None = Header(None)) -> dict:
    admin = _administrator(authorization)
    from dataclasses import asdict

    from core.api_storage_store import PolicyVersionConflict
    from tools.export.cards import ALL_CARD_TOOLS
    store = _api_storage_store(); store.initialize()
    changes = body.changes()
    if "enabled_tools" in changes:
        unknown = sorted(set(changes["enabled_tools"]) - ALL_CARD_TOOLS)
        if unknown:
            raise HTTPException(422, f"未知工具：{', '.join(unknown)}")
    if changes.get("preset") == "custom":
        current = store.get_display_policy()
        effective = changes.get("enabled_tools", current.enabled_tools)
        if not effective:
            raise HTTPException(422, "custom 预设需要至少选择一个工具（或改用 off）")
    try:
        updated = store.update_display_policy(
            changes, expected_version=body.expected_version, updated_by=admin["id"])
    except PolicyVersionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"policy": asdict(updated)}
