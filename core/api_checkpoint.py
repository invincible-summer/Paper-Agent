"""OpenAI-compatible conversation identity and structured Checkpoint storage.

The OpenAI channel is stateless at the protocol boundary, but the Agent needs
short-lived structured state to continue research across requests.  This module
never persists the request ``messages`` array, provider reasoning, API keys,
raw uploads, or complete PDF text.  It stores only the allow-listed
``ChatSession`` working state in the API-only SQLite/blob root.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
import unicodedata
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from agents.session import ChatSession
from core.api_storage_store import ApiStorageStore
from core.blocking import run_io_bound
from core.models import Paper, summaries_from_dicts
from core.storage_context import StorageContext, StoragePathError

CHECKPOINT_SCHEMA_VERSION = 1
MAX_CHECKPOINT_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
EXTERNAL_FIELD_BYTES = 256 * 1024
_SECRET_NAME = "checkpoint_hmac"


@dataclass(frozen=True)
class ApiCredentialPrincipal:
    """Non-secret identity used for API session partitioning."""

    credential_id: str
    key_id: str
    created_by: str
    source: str


@dataclass(frozen=True)
class CheckpointLoad:
    session: ChatSession
    created: bool
    session_id: str


class CheckpointVersionConflict(RuntimeError):
    """A stale Checkpoint writer lost an optimistic-lock race."""


def _normal_text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "").replace("\r\n", "\n").replace("\r", "\n"))


def _canonical_part(part: Any) -> dict[str, Any] | None:
    if not isinstance(part, Mapping):
        return {"type": "text", "text": _normal_text(part)}
    kind = str(part.get("type") or "text")
    if kind == "text":
        return {"type": "text", "text": _normal_text(part.get("text", ""))}
    if kind == "image_url":
        item = part.get("image_url") or {}
        if isinstance(item, Mapping):
            return {"type": "image_url", "url": _normal_text(item.get("url", ""))}
        return {"type": "image_url", "url": _normal_text(item)}
    if kind == "file":
        item = part.get("file") or {}
        if not isinstance(item, Mapping):
            item = {}
        # Keep only stable provider identifiers.  Do not persist file bytes or
        # signed headers; the values are HMAC input only and never stored.
        return {
            "type": "file",
            "file_id": _normal_text(item.get("file_id", "")),
            "url": _normal_text(item.get("url", "")),
            "filename": _normal_text(item.get("filename", "")),
        }
    if kind == "input_audio":
        item = part.get("input_audio") or {}
        if not isinstance(item, Mapping):
            item = {}
        return {
            "type": "input_audio",
            "url": _normal_text(item.get("url", "")),
            "format": _normal_text(item.get("format", "")),
        }
    # Unknown content parts are deliberately represented minimally rather than
    # guessed into the identity namespace.
    return {"type": kind, "value": _normal_text(part.get("text", ""))}


def canonical_message_chain(
    messages: Sequence[Mapping[str, Any]] | None, *, exclude_last_user: bool = True
) -> str:
    """Canonicalize a message chain for HMAC lookup without storing it.

    The final user message is excluded because it is the new turn input.  The
    last user entry is removed by index only; assistant/system messages after
    it remain part of the chain instead of being silently guessed away.
    """
    items = list(messages or [])
    if exclude_last_user:
        last_user = next(
            (index for index in range(len(items) - 1, -1, -1)
             if items[index].get("role") == "user"),
            None,
        )
        if last_user is not None:
            items.pop(last_user)
    canonical: list[dict[str, Any]] = []
    for message in items:
        if not isinstance(message, Mapping):
            continue
        role = _normal_text(message.get("role", ""))
        if role not in {"system", "user", "assistant"}:
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content_value: Any = [
                item for item in (_canonical_part(part) for part in content)
                if item is not None
            ]
        else:
            content_value = _normal_text(content)
        canonical.append({"role": role, "content": content_value})
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def principal_from_token(token: str, *, source: str = "development") -> ApiCredentialPrincipal:
    """Derive a non-secret fallback principal without retaining the token."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    key_id = f"{source}:{digest[:32]}"
    return ApiCredentialPrincipal(
        credential_id=key_id,
        key_id=key_id,
        created_by=source,
        source=source,
    )


def _summary_to_dict(summary: Any) -> dict[str, Any]:
    if isinstance(summary, Mapping):
        return dict(summary)
    if hasattr(summary, "to_dict"):
        return dict(summary.to_dict())
    return {"paper_id": str(getattr(summary, "paper_id", ""))}


def _attachment_ref(attachment: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "id", "filename", "char_count", "ext", "media_type",
        "multimodal_status", "element_count", "preview_url",
        "artifact_id", "sidecar_artifact_id", "relative_path", "text_relative_path",
        "source_file_id",
    )
    return {
        key: attachment.get(key, 0 if key in {"char_count", "element_count"} else "")
        for key in allowed
        if attachment.get(key) not in (None, "")
    }


def _paper_to_checkpoint(paper: Paper, context: StorageContext) -> dict[str, Any]:
    data = paper.to_dict()
    # Persist API-local PDF paths only as root-relative references. Web/foreign
    # absolute paths are never copied into the API Checkpoint.
    data["pdf_path"] = None
    if paper.pdf_path:
        try:
            relative = Path(paper.pdf_path).resolve().relative_to(context.root_dir.resolve())
            data["pdf_path"] = f"@api/{relative.as_posix()}"
        except (OSError, ValueError):
            pass
    data.pop("llm_reasoning", None)
    return data


def _summary_to_checkpoint(summary: Any) -> dict[str, Any]:
    data = _summary_to_dict(summary)
    data.pop("full_text", None)
    data.pop("embedding", None)
    data.pop("reasoning", None)
    data.pop("thinking", None)
    return data


def build_checkpoint_state(session: ChatSession) -> dict[str, Any]:
    """Return the allow-listed structured state; never include messages."""
    context = session.storage_context or StorageContext.openai_api()
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "topic": session.topic,
        "conception": session.conception,
        "language": session.language,
        "field_profile": session.field_profile,
        "papers": [_paper_to_checkpoint(paper, context) for paper in session.papers],
        "candidates": [_paper_to_checkpoint(paper, context) for paper in session.candidates],
        "paper_summaries": {
            paper_id: _summary_to_checkpoint(summary)
            for paper_id, summary in session.paper_summaries.items()
        },
        "map_data": session.map_data,
        "reading_path": session.reading_path,
        "literature_review": session.literature_review,
        "sub_directions": session.sub_directions,
        "search_queries": session.search_queries,
        "attachments": [_attachment_ref(item) for item in session.attachments],
        "loaded_skills": sorted(str(item) for item in session.loaded_skills),
        "full_read_count": int(session.full_read_count),
        "rag_session_id": session.session_id,
    }


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _decompress_limited(blob: bytes, limit: int = MAX_CHECKPOINT_UNCOMPRESSED_BYTES) -> bytes:
    decoder = zlib.decompressobj()
    raw = decoder.decompress(blob, limit + 1)
    if len(raw) > limit or decoder.unconsumed_tail or not decoder.eof:
        raise ValueError("checkpoint exceeds size limit")
    raw += decoder.flush()
    if len(raw) > limit:
        raise ValueError("checkpoint exceeds size limit")
    return raw


def _restore_state(state: Mapping[str, Any], *, session_id: str, context: StorageContext) -> ChatSession:
    if int(state.get("schema_version", 0)) != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("unsupported checkpoint schema")
    def papers(values: Any) -> list[Paper]:
        restored: list[Paper] = []
        for item in (values or []):
            if not isinstance(item, Mapping):
                continue
            data = dict(item)
            stored_path = str(data.get("pdf_path") or "")
            if stored_path.startswith("@api/"):
                data["pdf_path"] = str(context.resolve_relative(stored_path[5:]))
            else:
                data["pdf_path"] = None
            restored.append(Paper.from_dict(data))
        return restored
    session = ChatSession(
        channel="openai_api",
        storage_context=context,
        session_id=str(state.get("rag_session_id") or session_id),
        topic=str(state.get("topic") or ""),
        conception=str(state.get("conception") or ""),
        language=str(state.get("language") or "both"),
        field_profile=str(state.get("field_profile") or "general"),
        papers=papers(state.get("papers")),
        candidates=papers(state.get("candidates")),
        paper_summaries=summaries_from_dicts({
            str(key): dict(value)
            for key, value in (state.get("paper_summaries") or {}).items()
            if isinstance(value, Mapping)
        }),
        map_data=dict(state.get("map_data") or {}),
        reading_path=list(state.get("reading_path") or []),
        literature_review=str(state.get("literature_review") or ""),
        sub_directions=list(state.get("sub_directions") or []),
        search_queries=list(state.get("search_queries") or []),
        attachments=[dict(item) for item in (state.get("attachments") or []) if isinstance(item, Mapping)],
        loaded_skills={str(item) for item in (state.get("loaded_skills") or [])},
        full_read_count=int(state.get("full_read_count") or 0),
    )
    return session


class ApiCheckpointStore:
    """Session identity, checkpoint persistence and alias lifecycle."""

    def __init__(self, storage: ApiStorageStore | None = None):
        self.context = storage.context if storage else StorageContext.openai_api()
        self.storage = storage or ApiStorageStore(self.context)
        self.storage.initialize()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def _secret(self) -> bytes:
        return self.storage.get_or_create_secret(_SECRET_NAME)

    def alias_for(
        self,
        principal: ApiCredentialPrincipal,
        openai_user: str | None,
        messages: Sequence[Mapping[str, Any]] | None,
        *,
        complete: bool = False,
    ) -> str:
        user = _normal_text(openai_user or "")[:128]
        payload = "\0".join((
            principal.credential_id,
            user,
            canonical_message_chain(messages, exclude_last_user=not complete),
        ))
        return hmac.new(self._secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def stable_session_alias(
        self, principal: ApiCredentialPrincipal, provider_session_id: str
    ) -> str:
        """HMAC a provider sessionId without ever persisting the raw value."""
        value = _normal_text(provider_session_id).strip()[:128]
        payload = "stable-session\0" + principal.credential_id + "\0" + value
        return hmac.new(self._secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    async def _lock_for(self, session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(session_id, asyncio.Lock())

    def _new_session(self, principal: ApiCredentialPrincipal) -> ChatSession:
        now = time.time()
        session_id = uuid.uuid4().hex
        ttl = self.storage.get_policy().session_ttl_seconds
        with self.storage.connect() as conn:
            conn.execute(
                "INSERT INTO api_sessions"
                "(id, credential_id, rag_session_id, created_at, updated_at, last_accessed_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, principal.credential_id, session_id, now, now, now, now + ttl),
            )
            conn.commit()
        return ChatSession(
            channel="openai_api", storage_context=self.context,
            session_id=session_id, checkpoint_version=1,
        )

    def _find_alias_session(self, alias: str, principal: ApiCredentialPrincipal) -> str | None:
        now = time.time()
        with self.storage.connect() as conn:
            rows = conn.execute(
                "SELECT a.session_id, a.ambiguous, s.credential_id, s.expires_at, s.status "
                "FROM api_session_aliases a JOIN api_sessions s ON s.id = a.session_id "
                "WHERE a.lookup_hash = ? AND a.expires_at > ?",
                (alias, now),
            ).fetchall()
        if not rows or any(row[1] or row[2] != principal.credential_id or row[4] != "active" or row[3] <= now for row in rows):
            return None
        ids = {row[0] for row in rows}
        return next(iter(ids)) if len(ids) == 1 else None

    def _load_checkpoint_sync(self, session_id: str) -> ChatSession | None:
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT checkpoint_blob, checkpoint_schema_version, version, expires_at, rag_session_id "
                "FROM api_sessions WHERE id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        version = int(row[2])
        if row[3] <= time.time() or not row[0]:
            return ChatSession(
                channel="openai_api", storage_context=self.context,
                session_id=session_id, checkpoint_version=version,
            )
        try:
            payload = _decompress_limited(bytes(row[0]))
            state = json.loads(payload.decode("utf-8"))
            state = self._expand_external_fields(state)
            session = _restore_state(state, session_id=row[4], context=self.context)
            session.checkpoint_version = version
            return session
        except (OSError, ValueError, TypeError, json.JSONDecodeError, zlib.error, KeyError):
            with self.storage.connect() as conn:
                conn.execute("UPDATE api_sessions SET status = 'corrupt' WHERE id = ?", (session_id,))
                conn.commit()
            return None

    def get_or_create(
        self,
        principal: ApiCredentialPrincipal,
        openai_user: str | None,
        messages: Sequence[Mapping[str, Any]] | None,
        *,
        provider_session_id: str | None = None,
    ) -> CheckpointLoad:
        stable = _normal_text(provider_session_id).strip()[:128] if provider_session_id else ""
        alias = (self.stable_session_alias(principal, stable) if stable else
                 self.alias_for(principal, openai_user, messages))
        session_id = self._find_alias_session(alias, principal)
        if session_id:
            loaded = self._load_checkpoint_sync(session_id)
            if loaded is not None:
                now = time.time()
                with self.storage.connect() as conn:
                    expiry = now + self.storage.get_policy().session_ttl_seconds
                    conn.execute(
                        "UPDATE api_sessions SET last_accessed_at = ?, expires_at = ? WHERE id = ?",
                        (now, expiry, session_id),
                    )
                    conn.execute(
                        "UPDATE api_session_aliases SET expires_at = ? "
                        "WHERE lookup_hash = ? AND session_id = ?",
                        (expiry, alias, session_id),
                    )
                    conn.commit()
                return CheckpointLoad(loaded, False, session_id)
        session = self._new_session(principal)
        # Bind a supplied sessionId immediately, before tools or streaming work.
        # A later disconnect can therefore recover any partial Checkpoint.
        if stable:
            self.add_alias_hash_sync(session.session_id, alias, replace_expired=bool(stable))
        return CheckpointLoad(session, True, session.session_id)

    def _write_external_field(self, session_id: str, field: str, value: Any) -> dict[str, str]:
        raw = _json_bytes(value)
        compressed = zlib.compress(raw, level=6)
        digest = hashlib.sha256(compressed).hexdigest()
        name = hmac.new(self._secret(), f"{session_id}\0{digest}".encode(), hashlib.sha256).hexdigest()
        relative = Path("blobs") / "state_payload" / f"{name}.json.zlib"
        path = self.context.resolve_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.context.temp_dir / f"checkpoint-{uuid.uuid4().hex}.tmp"
        temp.write_bytes(compressed)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        os.chmod(path, 0o600)
        artifact_id = f"state_payload_{name}"
        now = time.time()
        with self.storage.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO api_artifacts"
                "(id, content_hash, scope, category, privacy_class, logical_name, public_alias, mime_type, relative_path, size_bytes, created_at, last_accessed_at, expires_at, status)"
                " VALUES (?, ?, ?, 'state_payload', 'private', ?, NULL, 'application/zlib', ?, ?, ?, ?, ?, 'active')",
                (artifact_id, digest, session_id, field, str(relative), len(compressed), now, now, now + self.storage.get_policy().session_ttl_seconds),
            )
            conn.execute(
                "INSERT OR IGNORE INTO api_session_artifacts(session_id, artifact_id, relation, created_at) VALUES (?, ?, 'checkpoint_field', ?)",
                (session_id, artifact_id, now),
            )
            conn.commit()
        return {"$artifact": artifact_id}

    def _expand_external_fields(self, state: dict[str, Any]) -> dict[str, Any]:
        expanded = dict(state)
        refs = [value.get("$artifact") for value in expanded.values()
                if isinstance(value, Mapping) and value.get("$artifact")]
        if not refs:
            return expanded
        with self.storage.connect() as conn:
            rows = conn.execute(
                "SELECT id, content_hash, relative_path FROM api_artifacts WHERE id IN (%s)"
                % ",".join("?" for _ in refs),
                refs,
            ).fetchall()
        paths = {
            row[0]: (self.context.resolve_relative(row[2]), str(row[1]))
            for row in rows
        }
        for key, value in list(expanded.items()):
            artifact_id = value.get("$artifact") if isinstance(value, Mapping) else None
            if not artifact_id:
                continue
            if artifact_id not in paths:
                raise FileNotFoundError("checkpoint payload artifact is missing")
            path, expected_hash = paths[artifact_id]
            compressed = path.read_bytes()
            if hashlib.sha256(compressed).hexdigest() != expected_hash:
                raise ValueError("checkpoint payload integrity check failed")
            raw = _decompress_limited(compressed)
            expanded[key] = json.loads(raw.decode("utf-8"))
        return expanded

    def save_checkpoint_sync(self, session: ChatSession, *, final: bool = False) -> None:
        if session.channel != "openai_api":
            return
        state = build_checkpoint_state(session)
        full_state = _json_bytes(state)
        if len(full_state) > MAX_CHECKPOINT_UNCOMPRESSED_BYTES:
            raise ValueError("checkpoint exceeds 8 MiB uncompressed limit")
        external: dict[str, Any] = {}
        for field, value in state.items():
            if field == "schema_version":
                continue
            raw = _json_bytes(value)
            if len(raw) > EXTERNAL_FIELD_BYTES:
                external[field] = self._write_external_field(session.session_id, field, value)
            else:
                external[field] = value
        external["schema_version"] = CHECKPOINT_SCHEMA_VERSION
        encoded = _json_bytes(external)
        if len(encoded) > MAX_CHECKPOINT_UNCOMPRESSED_BYTES:
            raise ValueError("checkpoint exceeds 8 MiB uncompressed limit")
        blob = zlib.compress(encoded, level=6)
        now = time.time()
        with self.storage.connect() as conn:
            version = session.checkpoint_version
            if version is None:
                row = conn.execute("SELECT version FROM api_sessions WHERE id = ?", (session.session_id,)).fetchone()
                if row is None:
                    raise ValueError("API session does not exist")
                version = int(row[0])
                session.checkpoint_version = version
            cursor = conn.execute(
                "UPDATE api_sessions SET checkpoint_blob = ?, checkpoint_schema_version = ?, "
                "checkpoint_uncompressed_bytes = ?, updated_at = ?, last_accessed_at = ?, expires_at = ?, version = version + 1 "
                "WHERE id = ? AND version = ? AND status = 'active'",
                (blob, CHECKPOINT_SCHEMA_VERSION, len(encoded), now, now, now + self.storage.get_policy().session_ttl_seconds, session.session_id, version),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise CheckpointVersionConflict("API session checkpoint version conflict")
            conn.commit()
            session.checkpoint_version = version + 1

    def add_alias_hash_sync(self, session_id: str, alias: str, *, replace_expired: bool = False) -> None:
        now = time.time()
        expiry = now + self.storage.get_policy().session_ttl_seconds
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if replace_expired:
                conn.execute(
                    "DELETE FROM api_session_aliases WHERE lookup_hash = ? AND expires_at <= ?",
                    (alias, now),
                )
            existing = conn.execute(
                "SELECT session_id FROM api_session_aliases WHERE lookup_hash = ?",
                (alias,),
            ).fetchall()
            other = any(row[0] != session_id for row in existing)
            if other:
                conn.execute(
                    "UPDATE api_session_aliases SET ambiguous = 1 WHERE lookup_hash = ?",
                    (alias,),
                )
            conn.execute(
                "INSERT INTO api_session_aliases(lookup_hash, session_id, expires_at, ambiguous, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(lookup_hash, session_id) DO UPDATE SET "
                "expires_at=excluded.expires_at, "
                "ambiguous=MAX(api_session_aliases.ambiguous, excluded.ambiguous)",
                (alias, session_id, expiry, 1 if other else 0, now),
            )
            conn.commit()

    def add_alias_sync(
        self,
        session_id: str,
        principal: ApiCredentialPrincipal,
        openai_user: str | None,
        messages: Sequence[Mapping[str, Any]] | None,
    ) -> None:
        alias = self.alias_for(principal, openai_user, messages, complete=True)
        self.add_alias_hash_sync(session_id, alias)

    async def checkpoint_callback(
        self,
        session: ChatSession,
        *,
        final: bool = False,
        final_answer: str | None = None,
    ) -> None:
        """Persist structured state during Agent progress; never create an alias."""
        del final, final_answer
        lock = await self._lock_for(session.session_id)
        async with lock:
            await run_io_bound(self.save_checkpoint_sync, session)

    async def finalize_turn(
        self,
        session: ChatSession,
        *,
        principal: ApiCredentialPrincipal,
        openai_user: str | None,
        messages: Sequence[Mapping[str, Any]] | None,
        final_answer: str,
    ) -> None:
        """Commit the final Checkpoint and only then establish the next-turn alias."""
        lock = await self._lock_for(session.session_id)
        async with lock:
            await run_io_bound(self.save_checkpoint_sync, session, final=True)
            final_messages = list(messages or [])
            final_messages.append({"role": "assistant", "content": _normal_text(final_answer)})
            await run_io_bound(
                self.add_alias_sync, session.session_id, principal, openai_user,
                final_messages,
            )


_STORE_CACHE: dict[str, ApiCheckpointStore] = {}


def get_api_checkpoint_store() -> ApiCheckpointStore:
    context = StorageContext.openai_api()
    key = str(context.root_dir)
    store = _STORE_CACHE.get(key)
    if store is None:
        store = ApiCheckpointStore(ApiStorageStore(context))
        _STORE_CACHE[key] = store
    return store
