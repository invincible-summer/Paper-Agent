"""In-process session memory for the OpenAI-compatible (/v1) endpoint.

The OpenAI protocol is stateless — the platform gateway sends the full
message array each turn. To keep conversation-local context (and tool
artifacts like searched papers) without any cross-conversation memory, we
key a session by a fingerprint of the conversation's first user message and
cache it in-process:

- TTL 2h + LRU 200 sessions: nothing persists across conversations.
- On a cache miss (e.g. server restart) the caller seeds a fresh session
  from the request's own message array, so context degrades to plain text
  history — never to another conversation's data.

Single-worker deployments only (uvicorn --workers 1), which matches the
rest of the in-process state (circuit breaker, LLM semaphores).
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict

from agents.session import ChatSession

_TTL_SECONDS = 2 * 3600
_MAX_SESSIONS = 200


class SessionMemory:
    def __init__(self, ttl_seconds: int = _TTL_SECONDS, max_sessions: int = _MAX_SESSIONS):
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._store: OrderedDict[str, tuple[float, ChatSession]] = OrderedDict()

    def get_or_create(self, key: str) -> tuple[ChatSession, bool]:
        """Return (session, created). created=True means the caller must seed it."""
        now = time.time()
        self._evict(now)
        hit = self._store.get(key)
        if hit is not None:
            _, session = hit
            self._store.move_to_end(key)
            self._store[key] = (now, session)
            return session, False
        session = ChatSession()
        self._store[key] = (now, session)
        self._evict(now)
        return session, True

    def _evict(self, now: float) -> None:
        for k in [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]:
            self._store.pop(k, None)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def __len__(self) -> int:
        return len(self._store)


_MEMORY = SessionMemory()


def get_memory() -> SessionMemory:
    return _MEMORY


def _message_text(content) -> str:
    """Text of a message regardless of str / content-array form."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def conversation_key(messages: list[dict], salt: str = "") -> str:
    """Stable fingerprint of a conversation: its FIRST user message.

    Must be identical on every turn of the same conversation (turn 1 carries
    one user message, turn N carries N), so only the first user message is
    used. `salt` mixes in a platform-provided caller identity (e.g. the
    OpenAI `user` request field) so two DIFFERENT users who open with the
    identical first message no longer collide into one shared session.
    Without a salt, two unrelated conversations that start with the same
    greeting still collide for up to the TTL — bounded (nothing crosses the
    TTL; no disk persistence) but avoidable whenever the gateway identifies
    the caller.
    """
    first = ""
    for m in messages or []:
        if m.get("role") == "user":
            first = _message_text(m.get("content"))[:500]
            break
    raw = f"{salt}\x00{first}" if salt else (first or "empty")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def seed_session_from_messages(session: ChatSession, messages: list[dict]) -> None:
    """Seed a fresh session with the request's message array (minus the last
    user message, which becomes the new turn input). Text-only, bounded."""
    source = list(messages or [])
    current_user_index = next(
        (i for i in range(len(source) - 1, -1, -1)
         if source[i].get("role") == "user"),
        None,
    )
    history: list[dict] = []
    for index, message in enumerate(source):
        if index == current_user_index:
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        history.append({"role": role, "content": _message_text(message.get("content"))})
    session.messages = history[-20:]
