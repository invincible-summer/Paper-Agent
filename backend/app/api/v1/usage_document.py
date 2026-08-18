"""Public usage-document and administrator image asset endpoints."""
from __future__ import annotations

import hashlib
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from core.blocking import run_io_bound
from core.usage_document_store import (
    UsageDocumentError,
    UsageDocumentVersionConflict,
    document_asdict,
    get_usage_document,
    update_usage_document,
)

router = APIRouter(tags=["usage-document"])


class UsageDocumentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)
    content: str = Field(max_length=500_000)


_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_ASSET_DIR = _PROJECT_ROOT / "data" / "usage_document" / "assets"
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_FILENAME_RE = re.compile(r"^[a-f0-9]{32}\.(?:png|jpg|jpeg|gif|webp)$")
_IMAGE_TYPES = {
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    ".webp": ("image/webp", b"RIFF"),
}


def _image_type(filename: str, raw: bytes) -> tuple[str, str] | None:
    suffix = Path(filename or "").suffix.lower()
    spec = _IMAGE_TYPES.get(suffix)
    if spec is None:
        return None
    media_type, magic = spec
    if isinstance(magic, tuple):
        valid = any(raw.startswith(item) for item in magic)
    else:
        valid = raw.startswith(magic)
        if suffix == ".webp":
            valid = valid and len(raw) >= 12 and raw[8:12] == b"WEBP"
    return (suffix[1:], media_type) if valid else None


def _persist_asset(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _administrator(authorization: str | None) -> dict:
    from app.api.v1.auth import current_user

    user = current_user(authorization)
    if user.get("role") != "administrator":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user


@router.get("/usage-document")
def get_public_usage_document() -> dict:
    return document_asdict(get_usage_document())


@router.put("/admin/usage-document")
def update_public_usage_document(
    body: UsageDocumentUpdateRequest,
    authorization: str | None = Header(None),
) -> dict:
    administrator = _administrator(authorization)
    try:
        document = update_usage_document(
            body.content,
            expected_version=body.expected_version,
            updated_by=str(administrator.get("username") or administrator.get("id") or "administrator"),
        )
    except UsageDocumentVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except UsageDocumentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"document": document_asdict(document)}


@router.get("/usage-document/assets/{filename}")
def get_usage_document_asset(filename: str):
    if not _FILENAME_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="invalid usage document asset")
    path = _ASSET_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="usage document asset not found")
    suffix = path.suffix.lower()
    media_type = _IMAGE_TYPES[suffix][0]
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/admin/usage-document/assets")
async def upload_usage_document_asset(
    file: UploadFile = File(...),
    authorization: str | None = Header(None),
) -> dict:
    _administrator(authorization)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="图片不能为空")
    if len(raw) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=422, detail="图片不能超过 10 MB")
    detected = _image_type(file.filename or "", raw)
    if detected is None:
        raise HTTPException(status_code=422, detail="仅支持 PNG、JPEG、GIF 或 WebP 图片")
    extension, media_type = detected
    # A random id avoids using user-controlled path components.  The digest is
    # only used for the response metadata and makes accidental duplicate
    # uploads diagnosable without exposing the original filename.
    asset_id = secrets.token_hex(16)
    filename = f"{asset_id}.{extension}"
    path = _ASSET_DIR / filename
    await run_io_bound(_persist_asset, path, raw)
    return {
        "filename": filename,
        "url": f"/api/v1/usage-document/assets/{filename}",
        "markdown": f"![图片说明](/api/v1/usage-document/assets/{filename})",
        "media_type": media_type,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
