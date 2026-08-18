from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import core.auth_settings_store as auth_store
import core.usage_document_store as document_store
import core.user_store as user_store
from app.api.v1 import usage_document as document_api


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "users.db"
    assets = tmp_path / "assets"
    monkeypatch.setattr(auth_store, "_DB_PATH", db)
    monkeypatch.setattr(user_store, "_DB_PATH", db)
    monkeypatch.setattr(document_store, "_DB_PATH", db)
    monkeypatch.setattr(document_api, "_ASSET_DIR", assets)
    auth_store.reset_cache()
    document_store.reset_cache()
    auth_store.update_auth_settings(
        {"auth_required": True},
        expected_version=auth_store.get_auth_settings().version,
        updated_by="test",
    )
    administrator, _ = user_store.bootstrap_administrator("administrator", "", "admin-password")
    normal = user_store.create_user("normal", "normal-password")
    yield {
        "admin": f"Bearer {user_store.issue_token(administrator['id'])}",
        "normal": f"Bearer {user_store.issue_token(normal['id'])}",
        "assets": assets,
    }
    auth_store.reset_cache()
    document_store.reset_cache()


def test_public_document_is_seeded_and_versioned(env):
    document = document_api.get_public_usage_document()
    assert document["title"] == "使用文档"
    assert "Paper Agent" in document["content"]
    assert document["version"] == 1

    updated = document_api.update_public_usage_document(
        document_api.UsageDocumentUpdateRequest(
            expected_version=document["version"], content="# 新文档\n\n支持 Markdown。"
        ),
        env["admin"],
    )
    assert updated["document"]["content"] == "# 新文档\n\n支持 Markdown。"
    assert updated["document"]["version"] == 2
    assert document_api.get_public_usage_document()["content"] == "# 新文档\n\n支持 Markdown。"


def test_document_update_is_admin_only_and_conflict_protected(env):
    current = document_api.get_public_usage_document()
    with pytest.raises(HTTPException) as normal_error:
        document_api.update_public_usage_document(
            document_api.UsageDocumentUpdateRequest(
                expected_version=current["version"], content="nope"
            ),
            env["normal"],
        )
    assert normal_error.value.status_code == 403

    document_api.update_public_usage_document(
        document_api.UsageDocumentUpdateRequest(
            expected_version=current["version"], content="new"
        ),
        env["admin"],
    )
    with pytest.raises(HTTPException) as conflict:
        document_api.update_public_usage_document(
            document_api.UsageDocumentUpdateRequest(
                expected_version=current["version"], content="stale"
            ),
            env["admin"],
        )
    assert conflict.value.status_code == 409


class _FakeUpload:
    def __init__(self, raw: bytes, filename: str):
        self.raw = raw
        self.filename = filename

    async def read(self) -> bytes:
        return self.raw


def _upload(raw: bytes, filename: str, authorization: str):
    return document_api.upload_usage_document_asset(
        _FakeUpload(raw, filename),
        authorization,
    )


def test_admin_image_upload_returns_markdown_and_rejects_unsafe_files(env):
    result = asyncio.run(_upload(b"\x89PNG\r\n\x1a\nvalid", "poster.png", env["admin"]))
    assert result["markdown"].startswith("![图片说明](/api/v1/usage-document/assets/")
    filename = result["filename"]
    assert (env["assets"] / filename).read_bytes().startswith(b"\x89PNG")
    assert document_api.get_usage_document_asset(filename).path == env["assets"] / filename

    with pytest.raises(HTTPException) as svg_error:
        asyncio.run(_upload(b"<svg>", "unsafe.svg", env["admin"]))
    assert svg_error.value.status_code == 422

    with pytest.raises(HTTPException) as normal_error:
        asyncio.run(_upload(b"\x89PNG\r\n\x1a\nvalid", "poster.png", env["normal"]))
    assert normal_error.value.status_code == 403


def test_asset_path_is_strict(env):
    with pytest.raises(HTTPException) as error:
        document_api.get_usage_document_asset("../secret.png")
    assert error.value.status_code == 400
