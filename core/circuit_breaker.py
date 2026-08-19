"""Per-tool circuit breaker (DESIGN D-068, agent-develop skill V4).

A tool that fails N times in a row is temporarily disabled so the model cannot
death-loop on a broken tool (skill: "Circuit breaker & MVTS"). Returns
`CIRCUIT_OPEN` instead of invoking the tool, which the prompt's Layer-3
recovery flow tells the model to stop calling that tool and try an alternative.

Design:
- Process-wide, per-tool singleton (mirrors RateLimitedLLM's global-cache pattern).
- Thread-safe via a plain mutex (state is touched from async tool execution).
- Half-open after the cooldown: the next call is allowed through; if it fails
  again the breaker re-opens, if it succeeds the failure count resets.

Keeping this small and separate from the tool protocol keeps the failure-driven
hardening visible and testable on its own.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from core.tool_protocol import ErrorCode, err
from core.trace import get_trace_dir  # noqa: F401  (kept for future trace wiring)


@dataclass
class _BreakerState:
    consecutive_failures: int = 0
    open_until: float = 0.0  # epoch seconds; 0 means closed


class CircuitBreaker:
    """Global per-tool circuit breaker registry.

    Threshold/cooldown follow the skill's defaults (3 fails -> 300s).
    """

    def __init__(self, threshold: int = 3, cooldown_s: float = 300.0):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._states: dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def _state(self, tool: str) -> _BreakerState:
        s = self._states.get(tool)
        if s is None:
            s = _BreakerState()
            self._states[tool] = s
        return s

    def is_open(self, tool: str) -> tuple[bool, float]:
        """Return (is_open, remaining_seconds)."""
        with self._lock:
            s = self._state(tool)
            now = time.time()
            if s.open_until and now < s.open_until:
                return True, round(s.open_until - now, 1)
            # cooldown elapsed -> half-open: allow one trial
            if s.open_until and now >= s.open_until:
                s.open_until = 0.0
            return False, 0.0

    def record_success(self, tool: str) -> None:
        with self._lock:
            s = self._state(tool)
            s.consecutive_failures = 0
            s.open_until = 0.0

    def record_failure(self, tool: str) -> None:
        with self._lock:
            s = self._state(tool)
            s.consecutive_failures += 1
            if s.consecutive_failures >= self.threshold:
                s.open_until = time.time() + self.cooldown_s

    def snapshot(self) -> dict[str, dict]:
        """Read-only state of every tracked tool (admin display)."""
        with self._lock:
            now = time.time()
            rows: dict[str, dict] = {}
            for tool, s in self._states.items():
                if s.open_until and now < s.open_until:
                    state = "open"
                elif s.consecutive_failures >= self.threshold:
                    state = "half_open"
                else:
                    state = "closed"
                rows[tool] = {
                    "tool": tool,
                    "state": state,
                    "consecutive_failures": s.consecutive_failures,
                    "remaining_seconds": round(max(0.0, s.open_until - now), 1),
                }
            return rows

    def force_close(self, tool: str) -> bool:
        """Administrative recovery: clear failures and any open window.

        Returns False when the tool was never tracked (nothing to recover).
        """
        with self._lock:
            s = self._states.get(tool)
            if s is None:
                return False
            s.consecutive_failures = 0
            s.open_until = 0.0
            return True

    def guard(self, tool: str):
        """Return a CIRCUIT_OPEN ToolResult if the breaker is open, else None.

        Call this before executing a tool. On None, proceed; on success call
        record_success, on failure call record_failure.
        """
        is_open, remaining = self.is_open(tool)
        if is_open:
            return err(
                tool, ErrorCode.CIRCUIT_OPEN,
                f"Tool '{tool}' temporarily disabled after {self.threshold} "
                f"consecutive failures. Retry in {remaining}s or use an "
                f"alternative tool.",
            )
        return None


# Module-level singleton so the breaker persists across turns/requests in one
# process, matching the "global" intent in the agent-develop skill.
GLOBAL_BREAKER = CircuitBreaker()


def get_breaker() -> CircuitBreaker:
    return GLOBAL_BREAKER
