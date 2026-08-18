"""Best-effort local import prewarm for the first-token path."""
from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

@dataclass
class PrewarmState:
    active_mode: str = "off"
    prewarm_state: str = "idle"  # idle/running/ready/failed
    duration_ms: float = 0.0
    last_error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

_state = PrewarmState()
_lock = threading.Lock()


def get_prewarm_state() -> dict:
    with _lock:
        return asdict(_state)


def _set(**changes) -> None:
    with _lock:
        for key, value in changes.items():
            setattr(_state, key, value)


def _prewarm_agent_stack(mode: str = "blocking") -> dict:
    started = time.monotonic()
    _set(active_mode=mode, prewarm_state="running", duration_ms=0.0,
         last_error="", started_at=time.time(), finished_at=0.0)
    try:
        # Keep this list local-only: no LLM/VLM invocation and no model download.
        importlib.import_module("agents.orchestrator")
        importlib.import_module("tools.export.cards")
        from core.llm import get_llm
        get_llm("light")
        state = "ready"
    except Exception as exc:  # startup must never depend on optional warmup
        state = "failed"
        _set(last_error=f"{type(exc).__name__}: {exc}"[:500])
        logger.exception("agent stack prewarm failed mode=%s", mode)
    duration = round((time.monotonic() - started) * 1000, 1)
    _set(prewarm_state=state, duration_ms=duration, finished_at=time.time())
    logger.info("agent_prewarm mode=%s state=%s duration_ms=%.1f", mode, state, duration)
    return get_prewarm_state()


def prewarm_agent_stack(mode: str = "blocking") -> dict:
    return _prewarm_agent_stack(mode)


def start_background_prewarm(mode: str = "background") -> asyncio.Task:
    return asyncio.create_task(asyncio.to_thread(_prewarm_agent_stack, mode))
