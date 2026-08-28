"""用户反馈提交入口：登录用户 / 游客 / 本地模式均可提交，仅管理员可查看。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.auth import current_user

router = APIRouter(tags=["feedback"])


class FeedbackCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: str = Field(default="其他", min_length=1, max_length=20)
    content: str = Field(min_length=1, max_length=4000)
    contact: str = Field(default="", max_length=120)


@router.post("/feedback", status_code=201)
def post_feedback(
    body: FeedbackCreateRequest,
    authorization: str | None = Header(None),
    x_guest_id: Annotated[str | None, Header()] = None,
) -> dict:
    user = current_user(authorization, x_guest_id)
    from core.feedback_store import FeedbackCooldownError, FeedbackError, create_feedback

    try:
        item = create_feedback(user, body.category, body.content, body.contact)
    except FeedbackCooldownError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except FeedbackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"item": item}
