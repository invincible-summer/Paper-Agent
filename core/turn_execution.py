"""Request-scoped execution budgets and cancellation for agent turns.

The web channel can omit this context.  OpenAI-compatible callers use a
bounded interactive budget so every stream terminates before the host's
external timeout and in-flight work can observe client cancellation.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable


ProgressCallback = Callable[[str], Any]


@dataclass(slots=True)
class TurnExecutionContext:
    channel: str = "web"
    soft_timeout_seconds: float | None = None
    hard_timeout_seconds: float | None = None
    progress_cb: ProgressCallback | None = None
    started_at: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    phase: str = "preparing"
    last_progress: str = ""

    @classmethod
    def openai_api(
        cls,
        *,
        progress_cb: ProgressCallback | None = None,
        soft_timeout_seconds: float = 95.0,
        hard_timeout_seconds: float = 105.0,
    ) -> "TurnExecutionContext":
        return cls(
            channel="openai_api",
            soft_timeout_seconds=soft_timeout_seconds,
            hard_timeout_seconds=hard_timeout_seconds,
            progress_cb=progress_cb,
        )

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def remaining_soft(self) -> float | None:
        if self.soft_timeout_seconds is None:
            return None
        return max(0.0, self.soft_timeout_seconds - self.elapsed())

    def remaining_hard(self) -> float | None:
        if self.hard_timeout_seconds is None:
            return None
        return max(0.0, self.hard_timeout_seconds - self.elapsed())

    def soft_expired(self) -> bool:
        remaining = self.remaining_soft()
        return remaining is not None and remaining <= 0

    def hard_expired(self) -> bool:
        remaining = self.remaining_hard()
        return remaining is not None and remaining <= 0

    def cancel(self) -> None:
        self.cancel_event.set()

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def set_phase(self, phase: str) -> None:
        self.phase = str(phase or "")[:80]

    def report(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        self.last_progress = text[:500]
        if self.progress_cb is not None:
            self.progress_cb(self.last_progress)

    def bounded_timeout(self, preferred: float | None, *, reserve: float = 0.0) -> float | None:
        """Return a timeout capped by the remaining soft budget.

        ``None`` means no timeout was configured.  A small positive floor lets
        callers enter ``asyncio.timeout`` without accidentally passing zero.
        """
        remaining = self.remaining_soft()
        if remaining is not None:
            remaining = max(0.0, remaining - max(0.0, reserve))
        values = [float(v) for v in (preferred, remaining) if v is not None]
        if not values:
            return None
        return max(0.001, min(values))
