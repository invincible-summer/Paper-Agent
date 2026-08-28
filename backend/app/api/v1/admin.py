"""Administrator-only management for long-lived Agent API credentials."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.api.v1.auth import current_user
from core.blocking import run_cpu_bound

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
    max_upload_bytes: int | None = Field(default=None, ge=1 * 1024 * 1024, le=200 * 1024 * 1024)
    export_ttl_seconds: int | None = Field(default=None, ge=3600, le=30 * 86400)
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
    "version": 3,
    "items": {
        "preset.privacy": {"title": "隐私优先", "does": "会话和上传保留 2 小时，导出 2 小时，语义/视觉缓存 30 天，Trace 关闭。", "affected": "API 会话、私有上传、导出、模型结果缓存。", "benefits": "磁盘和隐私暴露窗口最小。", "drawbacks": "跨日继续研究能力弱，重复下载、OCR、VLM 和模型费用更高。", "privacy": "最高", "disk": "最低", "latency_cost": "更常重新下载和理解文件", "continuity": "短", "effective": "新 TTL 立即用于后续访问；缩短 TTL 需要预览确认。", "fallback": "98% 保护仍强制生效。", "restore": "选择 balanced。"},
        "preset.balanced": {"title": "均衡（默认）", "does": "会话/上传 7 天、导出 24 小时、缓存 90 天、Trace 关闭。", "affected": "全部 API 侧临时研究数据。", "benefits": "兼顾连续性、费用和磁盘。", "drawbacks": "比隐私优先保留更多数据。", "privacy": "中", "disk": "中", "latency_cost": "通常无需频繁重算", "continuity": "7 天", "effective": "保存后立即生效。", "fallback": "磁盘阈值仍优先。", "restore": "点击恢复默认。"},
        "preset.performance": {"title": "性能优先", "does": "会话/上传 30 天、导出 7 天、缓存 180 天，metadata Trace 7 天。", "affected": "API 研究状态和可重建缓存。", "benefits": "最低重复下载/OCR/VLM 延迟和费用。", "drawbacks": "磁盘占用和保留窗口最大。", "privacy": "较低", "disk": "高", "latency_cost": "最低", "continuity": "30 天", "effective": "保存后立即生效。", "fallback": "85/95/98 阈值会覆盖保留期。", "restore": "选择 balanced。"},
        "ttl": {"title": "数据保留时间", "does": "分别控制 Checkpoint、上传、导出、缓存和 Trace 的过期时间。网络论文 PDF 不再下载；全文分析从上传文件开始。", "affected": "仅 /v1 API 数据，不影响自制前端。", "benefits": "可按隐私和复用需求取舍。", "drawbacks": "过短会导致重新下载、OCR、VLM 和上下文中断。", "privacy": "越短越高", "disk": "越短越低", "latency_cost": "越短重复成本越高", "continuity": "由 Session TTL 决定", "effective": "新写入立即采用；清理器处理旧数据。", "fallback": "磁盘压力可提前清理可重建数据。", "restore": "恢复 balanced。"},
        "max_upload_bytes": {"title": "API 远程文件上限（默认 200 MiB）", "does": "控制清小搭 /v1 的 file.url 远程下载和 API 私有附件保存大小；可下调但不能超过 200 MiB。", "affected": "仅 OpenAI-compatible API 文件输入，不影响 Web /chat/upload 的 20 MiB 限制。", "benefits": "与清小搭文件协议上限一致，并可按磁盘容量收紧。", "drawbacks": "过低时较大的 PDF、DOCX 或延期格式会被拒绝。", "privacy": "不改变", "disk": "限制单文件增长", "latency_cost": "大文件仍可能需要更长下载时间", "continuity": "修改立即影响后续请求；已保存文件不删除、不重新处理。", "effective": "保存后对下一次 API 下载/保存立即生效。", "fallback": "最小 1 MiB、最大 200 MiB；超限请求返回可读错误并清理临时文件。", "restore": "设为 200 MiB。"},
        "thresholds": {"title": "75 / 85 / 95 / 98 磁盘阈值", "does": "75% 告警并清过期；85% 连续清理可重建数据；95% 按策略暂停或紧急清理；98% 强制暂停文件重任务。", "affected": "API 上传、下载、深读、OCR、VLM、导出与缓存。", "benefits": "避免磁盘写满导致数据库和服务损坏。", "drawbacks": "压力时可能需要重新计算缓存，或要求用户重传已被紧急清理的私有上传。", "privacy": "无额外读取", "disk": "核心保护", "latency_cost": "清理后可能重算", "continuity": "文字问答始终保留", "effective": "下一次守卫/清理立即生效。", "fallback": "98% 不可关闭。", "restore": "75/85/95/98。"},
        "critical.pause_heavy": {"title": "95% 暂停文件重任务（默认）", "does": "暂停上传、下载、deep_read、OCR、VLM 和导出，普通文字问答继续。", "affected": "文件写入型 API 操作。", "benefits": "不提前删除未过期私有上传。", "drawbacks": "需扩容或清理后才能继续深读。", "privacy": "不增加删除", "disk": "停止增长", "latency_cost": "重任务暂不可用", "continuity": "文字聊天继续", "effective": "立即", "fallback": "98% 同样强制暂停。", "restore": "磁盘恢复后自动解除。"},
        "critical.emergency_evict": {"title": "95% 紧急清理", "does": "先删除公共缓存和生成物，再删除 24 小时未访问且非 in-flight 的最旧私有上传。", "affected": "可能影响仍在 TTL 内的 API 文件。", "benefits": "在小磁盘上尽量维持文件服务。", "drawbacks": "私有文件可能要求用户重新上传；旧网络论文文件不会重下。", "privacy": "更快删除", "disk": "释放最多", "latency_cost": "后续重算成本高", "continuity": "结构化 Checkpoint 尽量保留", "effective": "需预览和二次确认", "fallback": "in-flight/protected 数据不删；98% 仍暂停。", "restore": "切回 pause_heavy。"},
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


# ---------------------------------------------------------------------------
# Runtime web-auth settings (访问控制：账号登录/游客/注册/邮箱验证)
# ---------------------------------------------------------------------------


class AuthSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(gt=0)
    confirm_disable_auth: bool = False
    auth_required: bool | None = None
    guest_access: bool | None = None
    registration_open: bool | None = None
    email_requirement: Literal["none", "collect", "verify"] | None = None

    def changes(self) -> dict:
        return self.model_dump(
            exclude={"expected_version", "confirm_disable_auth"}, exclude_none=True)

    @model_validator(mode="after")
    def has_changes(self):
        if not self.changes():
            raise ValueError("至少提供一个设置修改字段")
        return self


class AuthTestEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    to: str = Field(min_length=3, max_length=254)


@router.get("/auth-settings")
def get_auth_settings(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from dataclasses import asdict

    from core.auth_settings_store import get_auth_settings as load
    from core.email_sender import smtp_status
    return {"settings": asdict(load()), "smtp": smtp_status()}


@router.put("/auth-settings")
def put_auth_settings(body: AuthSettingsUpdate,
                      authorization: str | None = Header(None)) -> dict:
    admin = _administrator(authorization)
    from dataclasses import asdict

    from core.auth_settings_store import (
        AuthSettingsVersionConflict, get_auth_settings, update_auth_settings,
    )
    from core.email_sender import smtp_configured

    changes = body.changes()
    current = get_auth_settings()
    if current.auth_required and changes.get("auth_required") is False \
            and not body.confirm_disable_auth:
        raise HTTPException(
            422, "关闭账号登录会让所有未登录访问立即变为本地用户并穿透数据隔离，"
                 "必须经二次确认（confirm_disable_auth=true）")
    if changes.get("email_requirement") == "verify" and not smtp_configured():
        raise HTTPException(
            422, "尚未配置 SMTP 发信（.env 中的 SMTP_* 项），无法启用强制邮箱验证")
    try:
        updated = update_auth_settings(
            changes, expected_version=body.expected_version, updated_by=admin["id"])
    except AuthSettingsVersionConflict as exc:
        raise HTTPException(409, "设置已被其他管理员修改，请刷新后重试") from None
    return {"settings": asdict(updated)}


@router.post("/auth-settings/test-email")
def post_auth_settings_test_email(body: AuthTestEmailRequest,
                                  authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from core.auth_settings_store import AuthSettingsError, normalize_email
    from core.email_sender import EmailSendError, send_test_email, smtp_configured

    if not smtp_configured():
        raise HTTPException(400, "尚未配置 SMTP 发信（.env 中的 SMTP_* 项）")
    try:
        to = normalize_email(body.to)
    except AuthSettingsError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        send_test_email(to)
    except EmailSendError as exc:
        raise HTTPException(502, str(exc)) from None
    return {"status": "sent"}


# ---------------------------------------------------------------------------
# OpenAI-compatible /v1 markdown-card display policy (清小搭 卡片展示策略)
# ---------------------------------------------------------------------------


class DisplayPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(gt=0)
    tool_cards_enabled: bool | None = None
    tool_error_cards_enabled: bool | None = None
    skill_card_enabled: bool | None = None
    research_map_svg_enabled: bool | None = None
    research_map_mermaid_enabled: bool | None = None
    research_map_html_enabled: bool | None = None
    research_map_markdown_enabled: bool | None = None

    def changes(self) -> dict:
        return self.model_dump(exclude={"expected_version"}, exclude_none=True)

    @model_validator(mode="after")
    def has_changes(self):
        if not self.changes():
            raise ValueError("至少提供一个策略修改字段")
        return self


@router.get("/display-policy")
async def get_display_policy(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from dataclasses import asdict

    def _load() -> dict:
        store = _api_storage_store(); store.initialize()
        return {"policy": asdict(store.get_display_policy())}

    return await run_cpu_bound(_load)


@router.put("/display-policy")
async def put_display_policy(body: DisplayPolicyUpdate,
                             authorization: str | None = Header(None)) -> dict:
    admin = _administrator(authorization)
    from dataclasses import asdict

    from core.api_storage_store import PolicyVersionConflict
    changes = body.changes()

    def _update() -> dict:
        store = _api_storage_store(); store.initialize()
        try:
            updated = store.update_display_policy(
                changes, expected_version=body.expected_version,
                updated_by=admin["id"],
            )
        except PolicyVersionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"policy": asdict(updated)}

    return await run_cpu_bound(_update)

# ---------------------------------------------------------------------------
# Runtime paper-search administration
# ---------------------------------------------------------------------------


class PaperSearchPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(gt=0)
    sources: dict[str, bool] | None = None
    search_deadline_seconds: int | None = Field(default=None, ge=10, le=30)
    per_source_timeout_seconds: int | None = Field(default=None, ge=3, le=30)
    routing_mode: Literal["smart", "all_enabled"] | None = None

    def changes(self) -> dict:
        return self.model_dump(exclude={"expected_version"}, exclude_none=True)

    @model_validator(mode="after")
    def has_changes(self):
        if not self.changes():
            raise ValueError("至少提供一个论文检索策略修改字段")
        return self


class PaperSearchDiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sources: list[str] | None = None
    capability: Literal["connectivity", "search", "abstract"] | None = None


class PaperCapabilityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(gt=0)
    enabled: bool


_PAPER_DIAGNOSTIC_SEMAPHORE = __import__("asyncio").Semaphore(1)


def _paper_policy_payload() -> dict:
    from core.config import get_settings
    from core.paper_search_settings_store import SOURCE_IDS, capability_defaults, get_latest_diagnostics, get_paper_search_policy, policy_dict
    from tools.search.diagnostics import source_catalog

    policy = get_paper_search_policy()
    static = get_settings().search
    default_sources = {name: bool(static.sources.get(name, True)) for name in SOURCE_IDS}
    defaults = {
        "sources": default_sources,
        "capabilities": capability_defaults(default_sources),
        "search_deadline_seconds": 30, "per_source_timeout_seconds": 12,
        "routing_mode": "smart",
    }
    quick_sources = dict(policy.sources)
    quick_sources.update({"openalex": False, "semantic_scholar": False, "core": False})
    latest = get_latest_diagnostics()
    catalog = source_catalog()
    disabled_by = {
        source: {capability: state.get("disabled_by") for capability, state in capabilities.items()}
        for source, capabilities in policy.capabilities.items()
    }
    disabled_reason = {
        source: {capability: state.get("reason") for capability, state in capabilities.items()}
        for source, capabilities in policy.capabilities.items()
    }
    return {
        "policy": policy_dict(policy), "capabilities": policy_dict(policy)["capabilities"],
        "defaults": defaults,
        "quick_preset": {"sources": quick_sources, "search_deadline_seconds": 30,
                         "per_source_timeout_seconds": 12, "routing_mode": "smart"},
        "source_catalog": catalog,
        "configuration_status": {
            row["id"]: row.get("configuration_status", "ready") for row in catalog
        },
        "disabled_by": disabled_by,
        "disabled_reason": disabled_reason,
        "diagnostic_summary": latest.get("capability"),
        "last_checked_at": latest.get("last_complete_at"),
    }


@router.get("/paper-search/policy")
async def get_admin_paper_search_policy(
    authorization: str | None = Header(None),
) -> dict:
    _administrator(authorization)
    return await run_cpu_bound(_paper_policy_payload)


@router.put("/paper-search/policy")
async def put_admin_paper_search_policy(
    body: PaperSearchPolicyUpdate,
    authorization: str | None = Header(None),
) -> dict:
    admin = _administrator(authorization)
    from core.paper_search_settings_store import (
        PaperSearchSettingsError, PaperSearchVersionConflict,
        policy_dict, update_paper_search_policy,
    )

    def _update() -> dict:
        try:
            policy = update_paper_search_policy(
                body.changes(), expected_version=body.expected_version,
                updated_by=admin["id"],
            )
        except PaperSearchVersionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except PaperSearchSettingsError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"policy": policy_dict(policy)}

    return await run_cpu_bound(_update)


@router.put("/paper-search/sources/{source}/capabilities/{capability}")
async def put_paper_source_capability(
    source: str, capability: str, body: PaperCapabilityUpdate,
    authorization: str | None = Header(None),
) -> dict:
    admin = _administrator(authorization)
    from core.paper_search_settings_store import (
        PaperSearchSettingsError, PaperSearchVersionConflict, policy_dict, set_source_capability,
    )
    try:
        policy = await run_cpu_bound(set_source_capability, source, capability, body.enabled,
                                     expected_version=body.expected_version, updated_by=admin["id"])
    except PaperSearchVersionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except PaperSearchSettingsError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"policy": policy_dict(policy), "capability": policy_dict(policy)["capabilities"][source][capability]}


@router.post("/paper-search/diagnostics/capabilities")
async def post_paper_capability_diagnostics(
    body: PaperSearchDiagnosticRequest,
    authorization: str | None = Header(None),
) -> dict:
    _administrator(authorization)
    import time
    from dataclasses import asdict
    from core.paper_search_settings_store import apply_diagnostic_results
    from tools.search.diagnostics import flatten_capability_diagnostics, run_platform_diagnostics

    if _PAPER_DIAGNOSTIC_SEMAPHORE.locked():
        raise HTTPException(409, "已有论文平台检测正在运行，请稍后重试")
    async with _PAPER_DIAGNOSTIC_SEMAPHORE:
        started_at = time.time()
        try:
            platforms = await run_platform_diagnostics(body.sources, body.capability)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        items = flatten_capability_diagnostics(platforms)
        for platform in platforms:
            platform.pop("_persist_capabilities", None)
        scope = (
            "single_capability" if body.capability
            else "complete_platforms" if body.sources
            else "complete_all"
        )
        run = await run_cpu_bound(
            apply_diagnostic_results, items, started_at=started_at,
            finished_at=time.time(), kind="capability", platforms=platforms,
            summary_metadata={
                "diagnostic_scope": scope,
                "requested_sources": body.sources or [],
            },
        )
        payload = asdict(run)
        payload["platforms"] = platforms
        payload["policy"] = _paper_policy_payload()["policy"]
        return payload


@router.get("/paper-search/diagnostics/latest")
async def get_paper_search_diagnostics_latest(
    authorization: str | None = Header(None),
) -> dict:
    _administrator(authorization)
    from core.paper_search_settings_store import get_latest_diagnostics

    return await run_cpu_bound(get_latest_diagnostics)



# ---------------------------------------------------------------------------
# Runtime per-tool time budgets (工具时限预算)
# ---------------------------------------------------------------------------

# Rich admin catalog: labels mirror the chat tool cards
# (tools/export/cards.py::_TOOL_META) so administrators never face a bare
# tool id. ``use_skill`` is an internal instruction-loading event and stays
# out of the tunable list.
_TOOL_BUDGET_CATALOG: list[dict] = [
    {"name": "search_papers", "label": "文献检索", "category": "检索与深读",
     "description": "多源聚合论文元数据与有效摘要，不探测或下载网络全文；含一次意图理解 LLM 调用，内部各阶段共享本预算。",
     "recommended_max": 70},
    {"name": "deep_read", "label": "深度阅读", "category": "检索与深读",
     "description": "仅解析用户上传的论文文件（结构解析/OCR/VLM 元素理解有缓存），生成结构化深读摘要；网络论文只能基于摘要回答。",
     "recommended_max": 90},
    {"name": "ask_papers", "label": "论文问答", "category": "检索与深读",
     "description": "基于网络论文有效摘要与用户上传全文的 RAG 检索回答问题；摘要不足时提示上传原文，不远程升级。",
     "recommended_max": 45},
    {"name": "research_map", "label": "研究地图", "category": "检索与深读",
     "description": "对会话论文集聚类并构建研究图谱（可选 OpenAlex 引文增强），只处理已检索到的论文。",
     "recommended_max": 60},
    {"name": "reading_path", "label": "阅读路径", "category": "检索与深读",
     "description": "为核心论文规划阅读顺序与依赖关系，以本地计算为主。",
     "recommended_max": 30},
    {"name": "write_review", "label": "文献综述", "category": "综述与写作",
     "description": "聚合全部有效网络摘要与用户上传全文，经过提纲、主题写作、综合和修订多阶段生成 Markdown/DOCX 综述。",
     "recommended_max": 75},
    {"name": "citation_export", "label": "参考文献导出", "category": "导出与引用",
     "description": "导出 BibTeX 参考文献文件，本地格式化，几乎不耗时。",
     "recommended_max": 30},
    {"name": "export_report", "label": "报告导出", "category": "导出与引用",
     "description": "确定性本地聚合会话成果并导出报告；综述固定生成 Markdown/DOCX，不调用模型。",
     "recommended_max": 45},
    {"name": "export_manuscript", "label": "文稿导出", "category": "导出与引用",
     "description": "确定性本地把已完成内容转换为 md/docx/tex 文件，不调用模型。",
     "recommended_max": 45},
    {"name": "bib_import", "label": "文献库导入", "category": "导出与引用",
     "description": "解析用户上传的 BibTeX 并导入会话论文库，以本地解析为主。",
     "recommended_max": 45},
    {"name": "check_structure", "label": "结构体检", "category": "质量核查",
     "description": "确定性本地解析上传草稿并检查章节树、比例、引用卫生等结构问题，零模型调用。",
     "recommended_max": 30},
    {"name": "check_format", "label": "格式检查", "category": "质量核查",
     "description": "确定性本地检查图表编号、引用风格、GB/T 7714、标题编号与格式要求，零模型调用。",
     "recommended_max": 30},
    {"name": "integrity_sweep", "label": "可靠性质检", "category": "质量核查",
     "description": "核查综述中每条引文与真实论文的对应关系，需要批量检索校验。",
     "recommended_max": 45},
    {"name": "exhibit_index", "label": "图表导览", "category": "图表与领域分析",
     "description": "读取用户上传论文解析或缓存的图/表/公式元素索引。",
     "recommended_max": 45},
    {"name": "explain_element", "label": "元素解读", "category": "图表与领域分析",
     "description": "读取用户上传论文元素缓存并讲解单个图/表/公式，轻量。",
     "recommended_max": 30},
    {"name": "field_census", "label": "领域普查", "category": "图表与领域分析",
     "description": "统计领域逐年发文与主题趋势并绘图，需要多轮检索聚合。",
     "recommended_max": 45},
]


class ToolBudgetPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(gt=0)
    budgets: dict[str, int] | None = None
    default_budget_seconds: int | None = Field(default=None, ge=5)
    reserve_seconds: int | None = Field(default=None, ge=2, le=30)
    # Cross-field rules (soft <= hard - 5, budgets <= hard) live in
    # core.tool_budget_store._validate as the single authority.
    api_turn_soft_seconds: int | None = Field(default=None, ge=30)
    api_turn_hard_seconds: int | None = Field(default=None, ge=35)

    def changes(self) -> dict:
        return self.model_dump(exclude={"expected_version"}, exclude_none=True)

    @model_validator(mode="after")
    def has_changes(self):
        if not self.changes():
            raise ValueError("至少提供一个工具时限修改字段")
        return self


class ToolBreakerRecoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool: str = Field(min_length=1, max_length=64)


def _tool_budget_payload() -> dict:
    from core.circuit_breaker import get_breaker
    from core.tool_budget_store import (
        CODE_TOOL_BUDGETS, get_tool_budget_policy, resolve_tool_budget,
    )

    policy = get_tool_budget_policy()
    catalog = []
    for entry in _TOOL_BUDGET_CATALOG:
        name = entry["name"]
        catalog.append({
            **entry,
            "default_seconds": float(CODE_TOOL_BUDGETS.get(name, policy.default_budget_seconds)),
            "current_seconds": resolve_tool_budget(policy, name),
            "overridden": name in policy.budgets,
        })
    return {
        "policy": {
            "budgets": dict(policy.budgets),
            "default_budget_seconds": policy.default_budget_seconds,
            "reserve_seconds": policy.reserve_seconds,
            "api_turn_soft_seconds": policy.api_turn_soft_seconds,
            "api_turn_hard_seconds": policy.api_turn_hard_seconds,
            "version": policy.version,
            "updated_by": policy.updated_by,
            "updated_at": policy.updated_at,
        },
        "catalog": catalog,
        "limits": {
            "min_seconds": 5, "max_seconds": policy.api_turn_hard_seconds,
            "min_reserve": 2, "max_reserve": 30,
            "gateway_timeout_seconds": 120,
            "min_api_turn_soft_seconds": 30,
            "max_api_turn_soft_seconds": policy.api_turn_hard_seconds - 5,
            "api_turn_soft_seconds": policy.api_turn_soft_seconds,
            "min_api_turn_hard_seconds": 35,
            "api_turn_hard_seconds": policy.api_turn_hard_seconds,
            "web_turn_soft_seconds": 240, "web_turn_hard_seconds": 300,
        },
        "breaker": {"threshold": 3, "cooldown_seconds": 300},
        "breaker_states": get_breaker().snapshot(),
    }


@router.get("/tool-budgets")
async def get_admin_tool_budgets(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    return await run_cpu_bound(_tool_budget_payload)


@router.put("/tool-budgets")
async def put_admin_tool_budgets(body: ToolBudgetPolicyUpdate,
                                 authorization: str | None = Header(None)) -> dict:
    admin = _administrator(authorization)
    from core.tool_budget_store import (
        ToolBudgetSettingsError, ToolBudgetVersionConflict, update_tool_budget_policy,
    )

    changes = body.changes()
    if "budgets" in changes:
        known = {entry["name"] for entry in _TOOL_BUDGET_CATALOG}
        unknown = sorted(set(changes["budgets"]) - known)
        if unknown:
            raise HTTPException(422, f"未知工具：{', '.join(unknown)}")

    def _update() -> dict:
        try:
            update_tool_budget_policy(
                changes, expected_version=body.expected_version, updated_by=admin["id"])
        except ToolBudgetVersionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ToolBudgetSettingsError as exc:
            raise HTTPException(422, str(exc)) from None
        return _tool_budget_payload()

    return await run_cpu_bound(_update)


@router.post("/tool-budgets/breaker/recover")
def post_tool_breaker_recover(body: ToolBreakerRecoverRequest,
                              authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from core.circuit_breaker import get_breaker

    breaker = get_breaker()
    breaker.force_close(body.tool)
    return {"tool": body.tool, "breaker_states": breaker.snapshot()}


# ---------------------------------------------------------------------------
# Runtime performance policy (startup warmup + research-map citations)
# ---------------------------------------------------------------------------

class PerformancePolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(gt=0)
    startup_prewarm_mode: Literal["blocking", "background", "role_first", "off"] | None = None
    map_citation_mode: Literal["fast", "quality", "off"] | None = None

    def changes(self) -> dict:
        return self.model_dump(exclude={"expected_version"}, exclude_none=True)

    @model_validator(mode="after")
    def has_changes(self):
        if not self.changes():
            raise ValueError("至少提供一个性能策略字段")
        return self


def _performance_policy_response(policy) -> dict:
    from dataclasses import asdict
    from core.prewarm import get_prewarm_state
    from core.paper_search_settings_store import get_paper_search_policy

    openalex_enabled = bool(get_paper_search_policy().sources.get("openalex", False))
    effective_citation_mode = policy.map_citation_mode if openalex_enabled else "off"
    warmup = get_prewarm_state()
    return {
        "settings": asdict(policy),
        "defaults": {"startup_prewarm_mode": "blocking", "map_citation_mode": "fast"},
        "openalex_enabled": openalex_enabled,
        "effective_map_citation_mode": effective_citation_mode,
        "map_citation_disabled_reason": "论文搜索策略已关闭 OpenAlex" if not openalex_enabled else "",
        "prewarm": warmup,
        "restart_required": warmup.get("active_mode") != policy.startup_prewarm_mode,
    }


@router.get("/performance-policy")
def get_performance_policy_api(authorization: str | None = Header(None)) -> dict:
    _administrator(authorization)
    from core.runtime_performance_policy import get_performance_policy
    return _performance_policy_response(get_performance_policy())


@router.put("/performance-policy")
def put_performance_policy_api(body: PerformancePolicyUpdate,
                               authorization: str | None = Header(None)) -> dict:
    admin = _administrator(authorization)
    from core.runtime_performance_policy import (
        PerformancePolicyVersionConflict, update_performance_policy,
    )
    try:
        policy = update_performance_policy(
            body.changes(), expected_version=body.expected_version, updated_by=admin["id"])
    except PerformancePolicyVersionConflict:
        raise HTTPException(409, "性能策略已被其他管理员修改，请刷新后重试") from None
    response = _performance_policy_response(policy)
    if "startup_prewarm_mode" in body.changes():
        response["restart_required"] = True
    return response


# ---------------------------------------------------------------------------
# User feedback administration (反馈仅管理员可见)
# ---------------------------------------------------------------------------


class FeedbackStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resolved: bool


@router.get("/feedback")
def get_feedback(
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
    authorization: str | None = Header(None),
) -> dict:
    _administrator(authorization)
    from core.feedback_store import list_feedback

    if status is not None and status not in {"open", "resolved"}:
        raise HTTPException(400, "status 必须是 open 或 resolved")
    return list_feedback(status=status, offset=offset, limit=limit)


@router.put("/feedback/{feedback_id}")
def put_feedback_status(
    feedback_id: str,
    body: FeedbackStatusUpdate,
    authorization: str | None = Header(None),
) -> dict:
    admin = _administrator(authorization)
    from core.feedback_store import set_feedback_status

    item = set_feedback_status(feedback_id, body.resolved, resolved_by=admin["id"])
    if item is None:
        raise HTTPException(404, "反馈不存在或已删除")
    return {"item": item}


@router.delete("/feedback/{feedback_id}")
def delete_feedback_api(
    feedback_id: str,
    authorization: str | None = Header(None),
) -> dict:
    _administrator(authorization)
    from core.feedback_store import delete_feedback

    if not delete_feedback(feedback_id):
        raise HTTPException(404, "反馈不存在或已删除")
    return {"status": "deleted", "id": feedback_id}
