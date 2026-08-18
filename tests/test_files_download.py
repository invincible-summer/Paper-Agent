"""Generated artifact download route: type whitelist and Unicode names."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.main import create_app


@pytest.fixture
def export_dir(tmp_path, monkeypatch):
    import core.auth_settings_store as auth_settings_store
    import app.api.v1.files as files_api

    # These unit tests call the route function directly; force the documented
    # local-development identity instead of depending on a developer's .env.
    monkeypatch.setattr(auth_settings_store, "_DB_PATH", tmp_path / "auth.db")
    auth_settings_store.reset_cache()
    current = auth_settings_store.get_auth_settings()
    if current.auth_required:
        auth_settings_store.update_auth_settings(
            {"auth_required": False},
            expected_version=current.version, updated_by="test")
    monkeypatch.setattr(files_api, "_EXPORT_DIR", tmp_path)
    return tmp_path


@pytest.mark.parametrize("name,mime,body", [
    ("20260813_163830_堂吉诃德的理性与非理性_韦伯卡里斯马理论视角下的论文骨架.docx",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04docx"),
    ("研究报告.md", "text/markdown", "# 报告".encode()),
    ("论文.tex", "application/x-tex", b"\\documentclass{ctexart}"),
])
def test_download_supported_files_with_unicode_name(export_dir, name, mime, body):
    from starlette.responses import FileResponse
    from app.api.v1.files import get_file

    (export_dir / name).write_bytes(body)
    resp = get_file(name)
    assert isinstance(resp, FileResponse)
    assert resp.status_code == 200
    assert resp.media_type.startswith(mime)
    assert "attachment" in resp.headers["content-disposition"].lower()
    assert "filename*=utf-8''" in resp.headers["content-disposition"].lower()
    assert Path(resp.path).read_bytes() == body


def test_download_rejects_unsupported_and_traversal(export_dir):
    from app.api.v1.files import get_file
    from fastapi import HTTPException

    (export_dir / "secret.pdf").write_bytes(b"pdf")
    with pytest.raises(HTTPException) as unsupported:
        get_file("secret.pdf")
    assert unsupported.value.status_code == 400
    with pytest.raises(HTTPException) as traversal:
        get_file("../secret.md")
    assert traversal.value.status_code == 400


def test_download_missing_supported_file_is_404(export_dir):
    from app.api.v1.files import get_file
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as missing:
        get_file("missing.docx")
    assert missing.value.status_code == 404
