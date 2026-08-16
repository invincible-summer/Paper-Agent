"""Element asset serving (figure/table/formula crop images).

The multimodal pipeline writes per-element PNG crops under
``data/assets/<paper_id>/<kind>_<ordinal>.png`` (tools/pdf/structure). This
route serves them to the frontend so figure thumbnails render inline in the
deep-read card and the explain_element result.

Security mirrors the /files route (DESIGN D-070): paper_id and filename are
basename-enforced (no path traversal), and only image extensions are served.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["elements"])

# Resolved per-request from settings so the route reads from the exact dir the
# structure parser writes (tools/pdf/structure writes under
# settings.reader.assets_dir, relative to the process CWD). Hard-coding an
# absolute path here would diverge when CWD differs between parser and server.
_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def _assets_root() -> Path:
    from core.config import get_settings
    return Path(get_settings().reader.assets_dir)


@router.get("/elements/assets/{paper_id}/{filename}")
def get_element_asset(paper_id: str, filename: str,
                      authorization: Annotated[str | None, Header()] = None,
                      x_guest_id: Annotated[str | None, Header()] = None):
    """Serve one element crop; upload-derived assets require their owner."""
    from tools.pdf.fetcher import _sanitize_filename_component as _sanitize

    # Uploaded-document element ids are ``upload:<uuid>`` and are sanitized to
    # ``upload_<uuid>`` in the asset directory. Network-paper crops are derived
    # from public OA papers and remain readable without a browser login.
    if paper_id.startswith("upload_"):
        import re as _re
        attachment_id = paper_id[len("upload_"):]
        if not _re.fullmatch(r"[a-f0-9]{32}", attachment_id):
            raise HTTPException(404, "asset not found")
        from app.api.v1.auth import current_user
        from core.web_artifact_store import owned_web_attachment
        user = current_user(authorization, x_guest_id)
        if owned_web_attachment(attachment_id, user["id"]) is None:
            if user["id"] != "local":
                raise HTTPException(404, "asset not found")
            from tools.ingest.attachments import find_original
            if find_original(attachment_id) is None:
                raise HTTPException(404, "asset not found")

    # filename must be a bare name (no path separators / traversal).
    fname = Path(filename).name
    if fname != filename or not fname:
        raise HTTPException(400, "invalid filename")
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    content_type = _CONTENT_TYPES.get(ext)
    if content_type is None:
        raise HTTPException(400, "unsupported asset type")
    # Sanitize paper_id the same way the structure parser names asset dirs, so
    # DOIs / arXiv ids containing ':' and '/' resolve correctly.
    pid = _sanitize(paper_id)
    fp = _assets_root() / pid / fname
    if not fp.is_file():
        raise HTTPException(404, "asset not found")
    return FileResponse(fp, media_type=content_type, filename=fname)
