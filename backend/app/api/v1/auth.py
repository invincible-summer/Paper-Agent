"""Auth routes for the multi-user frontend channel (/api/v1/auth/*).

Scope: ONLY the web frontend's /api/v1/* endpoints. The 清小搭 channel
(`/v1/*`) uses independent administrator-issued Agent API Keys (with the
legacy AGENT_API_KEY retained only as migration/recovery credential).

Three modes:
- AUTH_REQUIRED=false (default; local dev via start.sh): every caller is
  the built-in "local" user, everything behaves as before, no login UI.
- AUTH_REQUIRED=true + Bearer token: the authenticated account.
- AUTH_REQUIRED=true + no token: GUEST mode — the browser sends a
  persistent random ``X-Guest-Id`` (generated once, localStorage) and gets
  an isolated ``guest:<id>`` identity only when GUEST_ACCESS=true. Production
  can disable guest access while preserving the same multi-user architecture.

All four flags live in the runtime settings row (data/users.db, admin page
/api/v1/admin/auth-settings); the .env values only seed the row once.
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

router = APIRouter(prefix="/auth", tags=["auth"])

_GUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _flags():
    """Runtime auth flags (seeded from .env, owned by the admin page)."""
    from core.auth_settings_store import get_auth_settings
    return get_auth_settings()


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=254)
    verification_code: str = Field(default="", max_length=6)


class EmailCodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    return authorization[len("Bearer "):].strip()


def current_user(authorization: str | None, x_guest_id: str | None = None) -> dict:
    """Resolve the caller: local user (auth off) -> account (token) -> guest
    (browser-id header) -> 401."""
    if not _flags().auth_required:
        from core.user_store import LOCAL_USER
        return dict(LOCAL_USER)
    if authorization and authorization.startswith("Bearer "):
        from core.user_store import resolve_token
        user = resolve_token(authorization[len("Bearer "):].strip())
        if user is not None:
            return user
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    if _flags().guest_access and x_guest_id and _GUEST_ID_RE.fullmatch(x_guest_id):
        return {
            "id": f"guest:{x_guest_id}",
            "username": "guest",
            "display_name": "游客",
            "email": "",
            "role": "guest",
        }
    if _flags().guest_access:
        raise HTTPException(status_code=401, detail="需要登录或游客标识")
    raise HTTPException(status_code=401, detail="当前部署不允许游客访问，请先登录")


@router.get("/config")
async def auth_config() -> dict:
    """Public: tells the frontend whether to show the login page."""
    flags = _flags()
    return {
        "auth_required": flags.auth_required,
        "registration_open": flags.registration_open,
        "guest_access": flags.guest_access,
        "email_requirement": flags.email_requirement,
    }


@router.post("/email/send-code")
def send_email_code(body: EmailCodeRequest) -> dict:
    """Send a 6-digit registration verification code (verify mode only)."""
    flags = _flags()
    if not flags.auth_required or not flags.registration_open:
        raise HTTPException(403, "当前未开放注册")
    if flags.email_requirement != "verify":
        raise HTTPException(400, "当前未启用注册邮箱验证")
    from core.auth_settings_store import (
        CODE_TTL_SECONDS, email_code_cooldown_remaining, generate_email_code,
        hash_email_code, normalize_email, record_email_code,
    )
    from core.email_sender import EmailSendError, send_verification_email

    try:
        email = normalize_email(body.email)
    except Exception as exc:  # noqa: BLE001 — AuthSettingsError with message
        raise HTTPException(400, str(exc)) from None
    cooldown = email_code_cooldown_remaining(email)
    if cooldown > 0:
        raise HTTPException(429, f"发送过于频繁，请 {math.ceil(cooldown)} 秒后再试")
    code = generate_email_code()
    try:
        send_verification_email(email, code)
    except EmailSendError as exc:
        raise HTTPException(502, f"验证码邮件发送失败：{exc}") from None
    record_email_code(email, hash_email_code(code))
    return {"status": "sent", "expires_in": CODE_TTL_SECONDS}


@router.post("/register", status_code=201)
def register(body: Credentials) -> dict:
    flags = _flags()
    if not flags.auth_required:
        raise HTTPException(400, "本地模式无需注册（账号登录未开启）")
    if not flags.registration_open:
        raise HTTPException(403, "当前未开放注册，请联系管理员开通账号")
    from core.auth_settings_store import normalize_email, verify_email_code
    from core.user_store import UserStoreError, create_user, issue_token

    raw_email = body.email.strip()
    email = ""
    if flags.email_requirement == "verify" and not raw_email:
        raise HTTPException(422, "请填写注册邮箱")
    if flags.email_requirement == "collect" and not raw_email:
        raise HTTPException(422, "请填写注册邮箱")
    if raw_email:
        try:
            # 统一小写归一：验证码记录与邮箱唯一性检查都按小写比较。
            email = normalize_email(raw_email)
        except Exception as exc:  # noqa: BLE001 — AuthSettingsError with message
            raise HTTPException(400, str(exc)) from None
    verified = False
    if flags.email_requirement == "verify":
        if not body.verification_code.strip():
            raise HTTPException(422, "请输入邮箱验证码")
        try:
            verify_email_code(email, body.verification_code.strip())
        except Exception as exc:  # noqa: BLE001 — EmailCodeError with message
            raise HTTPException(400, str(exc)) from None
        verified = True
    try:
        user = create_user(body.username, body.password, body.display_name,
                           email=email, email_verified=verified)
    except UserStoreError as e:
        raise HTTPException(400, str(e)) from None
    return {"token": issue_token(user["id"]), "user": user}


@router.post("/login")
def login(body: Credentials) -> dict:
    if not _flags().auth_required:
        from core.user_store import LOCAL_USER
        return {"token": "", "user": dict(LOCAL_USER)}
    from core.user_store import authenticate, issue_token
    user = authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(401, "用户名或密码错误")
    return {"token": issue_token(user["id"]), "user": user}


@router.get("/me")
def me(
    authorization: str | None = Header(None),
    x_guest_id: str | None = Header(None),
) -> dict:
    return {"user": current_user(authorization, x_guest_id)}


@router.post("/change-password")
def password_change(
    body: PasswordChangeRequest,
    authorization: str | None = Header(None),
) -> dict:
    user = current_user(authorization)
    if user.get("role") not in {"user", "administrator"}:
        raise HTTPException(status_code=403, detail="当前身份不能修改密码")
    from core.user_store import UserStoreError, change_password

    try:
        change_password(user["id"], body.current_password, body.new_password)
    except UserStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"status": "changed", "reauthenticate": True}


@router.post("/logout")
def logout(authorization: str | None = Header(None)) -> dict:
    if _flags().auth_required:
        from core.user_store import revoke_token
        revoke_token(_bearer_token(authorization))
    return {"status": "ok"}
