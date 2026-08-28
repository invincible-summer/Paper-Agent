"""用户反馈：store 校验/冷却/生命周期、提交路由（含游客）、管理员专属管理端点。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import core.auth_settings_store as ass
import core.feedback_store as fs
import core.user_store as us
from app.core.config import settings

USER_A = {"id": "user-a", "username": "alice", "display_name": "爱丽丝", "role": "user"}
USER_B = {"id": "user-b", "username": "bob", "display_name": "鲍勃", "role": "user"}
GUEST = {"id": "guest:browser-abcd-1234", "username": "guest",
         "display_name": "游客", "role": "guest"}


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(fs, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(ass, "_DB_PATH", tmp_path / "users.db")
    ass.reset_cache()
    return fs


def _flags(monkeypatch, *, auth_required, guest_access=True):
    monkeypatch.setattr(ass, "get_auth_settings", lambda: SimpleNamespace(
        auth_required=auth_required, guest_access=guest_access,
        registration_open=False, email_requirement="none"))


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def test_create_and_list_feedback(isolated_store):
    first = isolated_store.create_feedback(USER_A, "问题报告", "检索结果里的 DOI 链接打不开", "a@example.com")
    second = isolated_store.create_feedback(USER_B, "功能建议", "希望支持导出 EndNote 格式")
    third = isolated_store.create_feedback(GUEST, "其他", "页面挺好用的")

    listed = isolated_store.list_feedback()
    assert listed["counts"] == {"open": 3, "resolved": 0, "total": 3}
    assert {item["id"] for item in listed["items"]} == {first["id"], second["id"], third["id"]}
    by_id = {item["id"]: item for item in listed["items"]}
    assert by_id[first["id"]]["category"] == "问题报告"
    assert by_id[first["id"]]["contact"] == "a@example.com"
    assert by_id[second["id"]]["contact"] == ""
    assert by_id[third["id"]]["username"] == "guest"
    assert all(item["status"] == "open" for item in listed["items"])

    # counts 始终是全局标签计数；items 跟随筛选条件
    assert isolated_store.list_feedback(status="open")["items"]
    assert isolated_store.list_feedback(status="resolved")["items"] == []
    assert isolated_store.list_feedback(status="resolved")["counts"]["total"] == 3


def test_create_feedback_validation_and_cooldown(isolated_store):
    with pytest.raises(fs.FeedbackError):
        isolated_store.create_feedback(USER_A, "无效类型", "内容")
    with pytest.raises(fs.FeedbackError):
        isolated_store.create_feedback(USER_A, "问题报告", "   ")
    with pytest.raises(fs.FeedbackError):
        isolated_store.create_feedback(USER_A, "问题报告", "长" * (fs._CONTENT_MAX_CHARS + 1))
    with pytest.raises(fs.FeedbackError):
        isolated_store.create_feedback(USER_A, "问题报告", "内容", contact="x" * 121)

    isolated_store.create_feedback(USER_A, "问题报告", "第一条")
    with pytest.raises(fs.FeedbackCooldownError):
        isolated_store.create_feedback(USER_A, "其他", "六十秒内的第二条")
    # 冷却按提交者隔离：另一个用户不受影响
    isolated_store.create_feedback(USER_B, "其他", "另一个用户立刻可以提交")


def test_status_and_delete_lifecycle(isolated_store):
    item = isolated_store.create_feedback(USER_A, "功能建议", "希望增加深色模式")

    resolved = isolated_store.set_feedback_status(item["id"], True, resolved_by="admin-1")
    assert resolved["status"] == "resolved"
    assert resolved["resolved_by"] == "admin-1"
    assert resolved["resolved_at"] is not None
    counts = isolated_store.list_feedback()["counts"]
    assert counts == {"open": 0, "resolved": 1, "total": 1}

    reopened = isolated_store.set_feedback_status(item["id"], False, resolved_by="admin-1")
    assert reopened["status"] == "open"
    assert reopened["resolved_at"] is None and reopened["resolved_by"] is None

    assert isolated_store.delete_feedback(item["id"]) is True
    assert isolated_store.delete_feedback(item["id"]) is False
    assert isolated_store.set_feedback_status(item["id"], True, resolved_by="admin-1") is None
    assert isolated_store.list_feedback()["counts"]["total"] == 0


# ---------------------------------------------------------------------------
# Submit route
# ---------------------------------------------------------------------------

def test_submit_route_validation_and_cooldown(isolated_store, monkeypatch):
    from fastapi import HTTPException
    from app.api.v1 import feedback as feedback_api

    _flags(monkeypatch, auth_required=False)
    body = feedback_api.FeedbackCreateRequest(
        category="问题报告", content="深读上传的 PDF 时报错", contact="")
    created = feedback_api.post_feedback(body, authorization=None, x_guest_id=None)
    assert created["item"]["content"] == "深读上传的 PDF 时报错"
    # 本地模式下无需登录即可提交，身份来自 LOCAL_USER
    assert created["item"]["role"] == "local"

    with pytest.raises(HTTPException) as bad_category:
        feedback_api.post_feedback(
            feedback_api.FeedbackCreateRequest(category="杂项", content="x"),
            authorization=None, x_guest_id=None)
    assert bad_category.value.status_code == 400

    with pytest.raises(HTTPException) as cooldown:
        feedback_api.post_feedback(
            feedback_api.FeedbackCreateRequest(category="其他", content="再一条"),
            authorization=None, x_guest_id=None)
    assert cooldown.value.status_code == 429


def test_submit_route_accepts_guest_identity(isolated_store, monkeypatch):
    from app.api.v1 import feedback as feedback_api

    _flags(monkeypatch, auth_required=True, guest_access=True)
    created = feedback_api.post_feedback(
        feedback_api.FeedbackCreateRequest(category="其他", content="游客留言"),
        authorization=None, x_guest_id="browser-abcd-1234")
    assert created["item"]["role"] == "guest"
    assert created["item"]["username"] == "guest"

    with pytest.raises(Exception) as anonymous:
        feedback_api.post_feedback(
            feedback_api.FeedbackCreateRequest(category="其他", content="未登录"),
            authorization=None, x_guest_id=None)
    assert anonymous.value.status_code == 401


def test_submit_route_rejects_guest_access_when_disabled(isolated_store, monkeypatch):
    from app.api.v1 import feedback as feedback_api

    _flags(monkeypatch, auth_required=True, guest_access=False)
    with pytest.raises(Exception) as denied:
        feedback_api.post_feedback(
            feedback_api.FeedbackCreateRequest(category="其他", content="x"),
            authorization=None, x_guest_id="browser-abcd-1234")
    assert denied.value.status_code == 401


# ---------------------------------------------------------------------------
# Administrator-only management endpoints
# ---------------------------------------------------------------------------

def test_admin_feedback_endpoints(isolated_store, monkeypatch):
    from fastapi import HTTPException
    from app.api.v1 import admin as admin_api

    admin, _ = us.bootstrap_administrator(
        "administrator", "administrator@administrator", "admin-password")
    normal = us.create_user("normal-user", "normal-password")
    admin_token = us.issue_token(admin["id"])
    normal_token = us.issue_token(normal["id"])

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "guest_access", False)
    ass.reset_cache()

    item = isolated_store.create_feedback(
        {"id": normal["id"], "username": "normal-user", "role": "user"},
        "问题报告", "管理员界面打不开")

    with pytest.raises(HTTPException) as forbidden:
        admin_api.get_feedback(authorization=f"Bearer {normal_token}")
    assert forbidden.value.status_code == 403
    with pytest.raises(HTTPException) as forbidden_put:
        admin_api.put_feedback_status(
            item["id"], admin_api.FeedbackStatusUpdate(resolved=True),
            authorization=f"Bearer {normal_token}")
    assert forbidden_put.value.status_code == 403

    listed = admin_api.get_feedback(authorization=f"Bearer {admin_token}")
    assert listed["counts"]["open"] == 1
    assert listed["items"][0]["content"] == "管理员界面打不开"

    with pytest.raises(HTTPException) as bad_status:
        admin_api.get_feedback(status="done", authorization=f"Bearer {admin_token}")
    assert bad_status.value.status_code == 400

    updated = admin_api.put_feedback_status(
        item["id"], admin_api.FeedbackStatusUpdate(resolved=True),
        authorization=f"Bearer {admin_token}")
    assert updated["item"]["status"] == "resolved"
    assert updated["item"]["resolved_by"] == admin["id"]

    with pytest.raises(HTTPException) as missing:
        admin_api.put_feedback_status(
            "no-such-id", admin_api.FeedbackStatusUpdate(resolved=False),
            authorization=f"Bearer {admin_token}")
    assert missing.value.status_code == 404

    deleted = admin_api.delete_feedback_api(item["id"], authorization=f"Bearer {admin_token}")
    assert deleted == {"status": "deleted", "id": item["id"]}
    with pytest.raises(HTTPException) as gone:
        admin_api.delete_feedback_api(item["id"], authorization=f"Bearer {admin_token}")
    assert gone.value.status_code == 404
