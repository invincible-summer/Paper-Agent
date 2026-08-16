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
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.core.config import settings  # noqa: E402

router = APIRouter(prefix="/auth", tags=["auth"])

_GUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=40)


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
    if not settings.auth_required:
        from core.user_store import LOCAL_USER
        return dict(LOCAL_USER)
    if authorization and authorization.startswith("Bearer "):
        from core.user_store import resolve_token
        user = resolve_token(authorization[len("Bearer "):].strip())
        if user is not None:
            return user
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    if settings.guest_access and x_guest_id and _GUEST_ID_RE.fullmatch(x_guest_id):
        return {
            "id": f"guest:{x_guest_id}",
            "username": "guest",
            "display_name": "游客",
            "email": "",
            "role": "guest",
        }
    if settings.guest_access:
        raise HTTPException(status_code=401, detail="需要登录或游客标识")
    raise HTTPException(status_code=401, detail="当前部署不允许游客访问，请先登录")


@router.get("/config")
def auth_config() -> dict:
    """Public: tells the frontend whether to show the login page."""
    return {
        "auth_required": settings.auth_required,
        "registration_open": settings.registration_open,
        "guest_access": settings.guest_access,
    }


@router.post("/register", status_code=201)
def register(body: Credentials) -> dict:
    if not settings.auth_required:
        raise HTTPException(400, "本地模式无需注册（AUTH_REQUIRED 未开启）")
    if not settings.registration_open:
        raise HTTPException(403, "当前未开放注册，请联系管理员开通账号")
    from core.user_store import UserStoreError, create_user, issue_token
    try:
        user = create_user(body.username, body.password, body.display_name)
    except UserStoreError as e:
        raise HTTPException(400, str(e)) from None
    return {"token": issue_token(user["id"]), "user": user}


@router.post("/login")
def login(body: Credentials) -> dict:
    if not settings.auth_required:
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
    if settings.auth_required:
        from core.user_store import revoke_token
        revoke_token(_bearer_token(authorization))
    return {"status": "ok"}
