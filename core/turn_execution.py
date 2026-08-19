"""Request-scoped execution budgets and cancellation for agent turns."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core.tool_budget_store import (
    CODE_FALLBACK_BUDGET,
    CODE_FALLBACK_RESERVE,
    CODE_TOOL_BUDGETS,
    ToolBudgetPolicy,
    resolve_tool_budget,
)

ProgressCallback = Callable[[str], Any]

# Public-tool admission classes for the /v1 channel.  The threshold is the
# minimum useful wall time; it is not another timeout stacked on the tool.
_LIGHT_TOOLS = {
    "ask_papers", "citation_export", "export_report", "check_structure",
    "check_format", "export_manuscript", "bib_import", "exhibit_index",
    "explain_element", "reading_path",
}
_MEDIUM_TOOLS = {"research_map", "field_census", "integrity_sweep"}
_HEAVY_TOOLS = {"search_papers", "deep_read", "write_review"}


@dataclass(frozen=True, slots=True)
class ToolAdmission:
    allowed: bool
    reason: str = ""
    effective_timeout_seconds: float | None = None
    configured_timeout_seconds: float | None = None
    minimum_seconds: float = 0.0


@dataclass(slots=True)
class ToolInvocationContext:
    """Effective one-tool deadline plus a recoverable partial result snapshot."""
    name: str
    configured_timeout_seconds: float
    effective_timeout_seconds: float
    timeout_kind: str
    deadline: float
    partial_result: Any | None = None

    def remaining(self, *, reserve: float = 0.0) -> float:
        return max(0.0, self.deadline - time.monotonic() - max(0.0, reserve))

    def publish_partial(self, result: Any) -> None:
        self.partial_result = result


@dataclass(slots=True)
class TurnExecutionContext:
    channel: str = "web"
    soft_timeout_seconds: float | None = None
    hard_timeout_seconds: float | None = None
    progress_cb: ProgressCallback | None = None
    tool_budget_policy: ToolBudgetPolicy | None = None
    started_at: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    phase: str = "preparing"
    last_progress: str = ""
    last_timeout_kind: str = ""
    timeout_count: int = 0
    public_tools_started: int = 0
    stop_public_tools: bool = False
    stop_reason: str = ""

    @classmethod
    def openai_api(
        cls,
        *,
        progress_cb: ProgressCallback | None = None,
        soft_timeout_seconds: float = 95.0,
        hard_timeout_seconds: float = 105.0,
        tool_budget_policy: ToolBudgetPolicy | None = None,
    ) -> "TurnExecutionContext":
        return cls(
            channel="openai_api",
            soft_timeout_seconds=soft_timeout_seconds,
            hard_timeout_seconds=hard_timeout_seconds,
            progress_cb=progress_cb,
            tool_budget_policy=tool_budget_policy,
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

    @property
    def reserve_seconds(self) -> float:
        if self.tool_budget_policy is not None:
            return float(self.tool_budget_policy.reserve_seconds)
        try:
            from core.tool_budget_store import get_turn_reserve
            return float(get_turn_reserve())
        except Exception:
            return CODE_FALLBACK_RESERVE

    def configured_tool_budget(self, name: str) -> float:
        if self.tool_budget_policy is not None:
            return resolve_tool_budget(self.tool_budget_policy, name)
        try:
            from core.tool_budget_store import get_tool_budget
            return float(get_tool_budget(name))
        except Exception:
            return float(CODE_TOOL_BUDGETS.get(name, CODE_FALLBACK_BUDGET))

    def minimum_start_seconds(self, name: str) -> float:
        if name in _HEAVY_TOOLS:
            return 15.0
        if name in _MEDIUM_TOOLS:
            return 10.0
        return 5.0

    def budget_hint(self) -> str:
        remaining = self.remaining_soft()
        remaining_text = "不限" if remaining is None else f"{remaining:.1f}s"
        return (
            f"[本轮动态预算] 剩余软时限 {remaining_text}；最终回答预留 "
            f"{self.reserve_seconds:.0f}s；已启动公开工具 {self.public_tools_started}/3。"
            "只选择当前用户明确要求或刚确认的第一项必要操作；预算截断后直接总结。"
        )

    def admit_public_tool(self, name: str, *, batch_index: int = 0) -> ToolAdmission:
        """Apply the stricter /v1 admission gate without changing Web behavior."""
        configured = self.configured_tool_budget(name)
        minimum = self.minimum_start_seconds(name)
        if self.channel != "openai_api":
            return ToolAdmission(True, effective_timeout_seconds=configured,
                                 configured_timeout_seconds=configured,
                                 minimum_seconds=minimum)
        if batch_index > 0:
            return ToolAdmission(False, "一个模型决策批次最多执行一个公开工具。",
                                 configured_timeout_seconds=configured,
                                 minimum_seconds=minimum)
        if self.stop_public_tools:
            return ToolAdmission(False, self.stop_reason or "本轮工具链已关闭。",
                                 configured_timeout_seconds=configured,
                                 minimum_seconds=minimum)
        if self.public_tools_started >= 3:
            return ToolAdmission(False, "本轮最多启动三个公开工具。",
                                 configured_timeout_seconds=configured,
                                 minimum_seconds=minimum)
        remaining = self.remaining_soft()
        available = configured if remaining is None else max(0.0, remaining - self.reserve_seconds)
        if self.public_tools_started == 0:
            if available < minimum:
                return ToolAdmission(False, f"剩余可用时间不足 {minimum:.0f} 秒，无法安全启动该工具。",
                                     configured_timeout_seconds=configured,
                                     minimum_seconds=minimum)
            effective = min(configured, available)
        else:
            if remaining is not None and remaining < configured + self.reserve_seconds:
                return ToolAdmission(False, "剩余时间放不下该工具的完整管理员预算和最终回答预留。",
                                     configured_timeout_seconds=configured,
                                     minimum_seconds=minimum)
            effective = configured
        return ToolAdmission(True, effective_timeout_seconds=max(0.001, effective),
                             configured_timeout_seconds=configured,
                             minimum_seconds=minimum)

    def mark_public_tool_started(self) -> None:
        self.public_tools_started += 1

    def close_public_tools(self, reason: str) -> None:
        self.stop_public_tools = True
        self.stop_reason = str(reason or "本轮工具链已关闭。")[:300]

    def timeout_for(self, preferred: float | None, *, reserve: float = 0.0) -> tuple[float | None, str]:
        remaining = self.remaining_soft()
        if remaining is None:
            return (float(preferred) if preferred is not None else None, "tool_budget")
        available = max(0.0, remaining - max(0.0, reserve))
        if preferred is None or available <= float(preferred):
            return max(0.001, available), "turn_budget"
        return max(0.001, float(preferred)), "tool_budget"

    def bounded_timeout(self, preferred: float | None, *, reserve: float = 0.0) -> float | None:
        remaining = self.remaining_soft()
        if remaining is not None:
            remaining = max(0.0, remaining - max(0.0, reserve))
        values = [float(v) for v in (preferred, remaining) if v is not None]
        if not values:
            return None
        return max(0.001, min(values))
