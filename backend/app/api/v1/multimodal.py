"""Multimodal message parsing for the OpenAI-compatible endpoint (清小搭).

Input content arrays (OpenAI multimodal format):
  text        -> used directly
  file        -> {file: {url|file_id, filename}} downloaded + text-extracted
                 (tools/ingest), registered as a session attachment and
                 indexed into the session RAG
  image_url   -> reserved: routed through the MediaAdapter protocol
  input_audio -> reserved: routed through the MediaAdapter protocol

MediaAdapter is the reserved extension point: when the configured LLM gains
vision/audio capability, an adapter implementation turns media into
descriptions/embeddings without touching this module's callers. With the
current text-only model the adapter is None and the user gets an explicit,
honest degradation note.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.blocking import run_cpu_bound

logger = logging.getLogger(__name__)

_UPLOAD_DIR = Path(__file__).resolve().parents[4] / "data" / "uploads"


@dataclass
class MediaPart:
    type: str            # "image" | "audio" | "file"
    url: str = ""
    filename: str = ""
    format: str = ""     # audio format: wav/mp3/m4a/webm
    file_id: str = ""    # cannot be resolved without a platform resolver API


class MediaAdapter(Protocol):
    """Reserved interface for vision/audio-capable backends."""

    async def describe(self, part: MediaPart) -> str | None:
        """Turn a media part into a textual description; None = unsupported."""
        ...


def get_media_adapter() -> MediaAdapter | None:
    """Return the active media adapter, or None when no vision backend is wired.

    When the multimodal VLM (core.multimodal.vision_client, MULTIMODAL_* config)
    is configured, a VisionMediaAdapter describes uploaded images via that VLM.
    When it isn't, None is returned and callers degrade honestly (the user gets
    a note that the current setup can't see images). Audio stays unsupported.
    """
    from core.multimodal.vision_client import get_vision_client

    if get_vision_client() is None:
        return None
    return VisionMediaAdapter()


async def _resolve_image_bytes(url: str) -> bytes | None:
    """Turn an image_url into raw bytes. Handles data: URIs and http(s).

    http(s) downloads go through the SSRF-guarded, size-capped downloader used
    for file ingestion, so chat-uploaded images get the same safety as PDFs.
    Returns None on any failure (caller degrades to "can't see this image").
    """
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("data:"):
        # data:image/png;base64,<payload>
        try:
            header, b64 = url.split(",", 1)
            import base64
            return base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return None
    if url.startswith(("http://", "https://")):
        try:
            from tools.ingest.downloader import download_bytes
            raw, _ct = await download_bytes(url)
            return raw
        except Exception:  # noqa: BLE001
            return None
    return None


class VisionMediaAdapter:
    """Describes uploaded images via the configured VLM vision client.

    Audio parts return None (unsupported). Images are sent through the same
    VisionClient the paper-element pipeline uses (with the figure prompt, which
    produces a general structured description), so chat uploads and paper
    figures are understood by one consistent model.
    """

    async def describe(self, part: MediaPart) -> str | None:
        if part.type != "image":
            return None
        image_bytes = await _resolve_image_bytes(part.url)
        if not image_bytes:
            return None
        from core.multimodal.vision_client import get_vision_client
        from core.prompts.registry import get
        client = get_vision_client()
        if client is None:
            return None
        pdef = get("vision.figure")
        try:
            result = await client.analyze(
                image_bytes, "image", pdef.text,
                prompt_version=pdef.version, expect_json=True,
            )
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(result, dict):
            return None
        desc = (result.get("description") or "").strip()
        components = result.get("components") or []
        text = desc
        if components:
            text += "；包含：" + "、".join(str(c) for c in components[:8])
        return text or None


def extract_user_content(message: dict) -> tuple[str, list[MediaPart]]:
    """Split an OpenAI message into (text, media parts)."""
    content = message.get("content")
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []

    texts: list[str] = []
    media: list[MediaPart] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            texts.append(part.get("text", ""))
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            media.append(MediaPart(type="image", url=url))
        elif ptype == "input_audio":
            audio = part.get("input_audio") or {}
            media.append(MediaPart(type="audio", url=audio.get("url", ""),
                                   format=audio.get("format", "")))
        elif ptype == "file":
            f = part.get("file") or {}
            media.append(MediaPart(type="file", url=f.get("url", ""),
                                   filename=f.get("filename", ""),
                                   file_id=f.get("file_id", "")))
    return " ".join(t for t in texts if t).strip(), media


async def process_media_parts(
    parts: list[MediaPart], *, storage_context=None, session_id: str = "",
) -> tuple[list[str], list[dict], list[str]]:
    """Persist OpenAI-compatible file/image parts through unified ingestion.

    File and image inputs become ordinary session attachments and intentionally
    incur no VLM call here. Audio remains unsupported and degrades explicitly.
    """
    notes: list[str] = []
    attachments: list[dict] = []
    errors: list[str] = []
    if not parts:
        return notes, attachments, errors

    from tools.ingest.attachments import (
        AttachmentError, save_attachment, save_attachment_from_path,
    )
    from tools.ingest.downloader import download_bytes, download_to_temp

    for part in parts:
        if part.type == "audio":
            notes.append(
                f"[用户上传了音频（{part.format or '未知格式'}），当前不支持音频解析；"
                "请用户提供文字转写。]"
            )
            continue
        if part.type not in {"file", "image"}:
            continue
        if part.type == "file" and part.file_id and not part.url:
            errors.append(
                f"文件 {part.filename or part.file_id} 仅提供了 file_id；"
                "当前清小搭接入文档未提供外部 Agent 解析 file_id 的接口，"
                "请改为传入 file.url。"
            )
            continue
        if not part.url:
            errors.append(f"{part.filename or '媒体文件'} 缺少下载地址，无法读取。")
            continue
        try:
            temp_path = None
            if part.url.startswith("data:"):
                header, payload = part.url.split(",", 1)
                import base64
                raw = base64.b64decode(payload)
                content_type = header[5:].split(";", 1)[0]
            else:
                raw = None
                if storage_context is not None and storage_context.channel == "openai_api":
                    temp_path, content_type = await download_to_temp(
                        part.url, temp_dir=storage_context.temp_dir
                    )
                else:
                    temp_path = None
                    raw, content_type = await download_bytes(part.url)
            filename = part.filename
            if not filename:
                if part.type == "image":
                    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(
                        content_type.split(";", 1)[0].lower(), "png")
                    filename = f"image.{ext}"
                else:
                    filename = Path(part.url.split("?", 1)[0]).name or "upload"
            try:
                if temp_path is not None:
                    attachment = await run_cpu_bound(
                        save_attachment_from_path, temp_path, filename,
                        content_type=content_type, storage_context=storage_context,
                        session_id=session_id,
                    )
                else:
                    attachment = await run_cpu_bound(
                        save_attachment, raw or b"", filename,
                        content_type=content_type, storage_context=storage_context,
                        session_id=session_id,
                    )
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            attachments.append(attachment)
            notes.append(
                f"[用户上传了{attachment['filename']}，已保存到当前会话；"
                "如问题涉及图、表、公式，将按需进行多模态理解。]"
            )
        except (AttachmentError, ValueError) as e:
            errors.append(f"文件 {part.filename or part.url[:80]} 读取失败：{e}")
        except Exception as e:  # noqa: BLE001
            logger.debug("media ingest failed: %s", e)
            errors.append(f"文件 {part.filename or part.url[:80]} 读取失败。")
    return notes, attachments, errors
