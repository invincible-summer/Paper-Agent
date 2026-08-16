"""Download endpoint for generated artifact files (x_soda.attachments).

Files are written under data/exports/ by tools/export/report.py and served
here by bare filename (basename-enforced, no traversal). 清小搭 pulls the
file once and re-hosts it, so the URL only needs to work short-term.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["files"])

_EXPORT_DIR = Path(__file__).resolve().parents[4] / "data" / "exports"

_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".tex": "application/x-tex",
    ".svg": "image/svg+xml",
}


@router.get("/files/{filename}")
def get_file(filename: str, authorization: Annotated[str | None, Header()] = None,
             x_guest_id: Annotated[str | None, Header()] = None):
    bare = Path(filename).name
    if bare != filename or not bare:
        raise HTTPException(400, "invalid filename")
    media_type = _CONTENT_TYPES.get(Path(bare).suffix.lower())
    if media_type is None:
        raise HTTPException(400, "unsupported file type")
    # API public aliases live under data/openai_api and expire independently.
    # Resolve them first, but never create API runtime storage for an unrelated
    # web-only file request.
    try:
        from core.api_artifact_store import ApiArtifactStore
        from core.api_storage_store import ApiStorageStore
        from core.storage_context import StorageContext
        context = StorageContext.openai_api()
        if context.state_db.is_file():
            artifact_store = ApiArtifactStore(ApiStorageStore(context))
            artifact = artifact_store.resolve_public_alias(bare)
            if artifact is not None:
                return FileResponse(
                    artifact.path, media_type=artifact.mime_type,
                    filename=artifact.logical_name,
                )
            if artifact_store.has_public_alias(bare):
                raise HTTPException(404, "file not found")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 -- legacy web exports remain available.
        pass
    fp = _EXPORT_DIR / bare
    if not fp.is_file():
        raise HTTPException(404, "file not found")

    # Browser-channel exports are private to the web account that created the
    # tool result.  API public aliases returned above intentionally remain
    # unauthenticated so 清小搭 can fetch them.
    from app.api.v1.auth import current_user
    from core.web_artifact_store import owned_web_export, register_web_artifact
    user = current_user(authorization, x_guest_id)
    if not owned_web_export(bare, user["id"]):
        # Auth-disabled local development may still have pre-index exports.
        # Bind them to the only local identity on first access; authenticated
        # production users must have an explicit owner record/history reference.
        if user["id"] != "local":
            raise HTTPException(404, "file not found")
        register_web_artifact("export", bare, user["id"])
    return FileResponse(fp, media_type=media_type, filename=bare)
