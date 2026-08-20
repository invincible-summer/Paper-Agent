"""Unified tool response protocol (DESIGN D-067, agent-develop skill V1).

Every chat-agent tool returns the same JSON shape so the agent is diagnosable:
the model checks `status`; on `error` it reads `error.code` and branches, instead
of guessing from a bare string. This is principle #4 of the agent-develop skill:
"Protocol before trickery, structure before rhetoric."

Shape:
    {
      "status": "success" | "partial" | "error",
      "data":   { ...structured payload... },
      "text":   "human/LLM-readable summary",
      "error":  {"code": "...", "message": "..."} | None,
      "stats":  {"time_ms": ...} | None,
      "tool":   "search" | "graph" | ...
    }

For backward compatibility with the frontend (which reads `result.summary`)
and the existing Reflector rules, `to_dict()` also flattens `data` keys to the
top level and aliases `text` as `summary`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    """Machine-readable error codes the model can branch on."""
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NO_PAPERS = "NO_PAPERS"
    NO_REVIEW = "NO_REVIEW"
    NO_TOOL = "NO_TOOL"
    TIMEOUT = "TIMEOUT"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    TOOL_ERROR = "TOOL_ERROR"
    STORAGE_PRESSURE = "STORAGE_PRESSURE"
    SOURCE_CAPABILITY_DISABLED = "SOURCE_CAPABILITY_DISABLED"


@dataclass
class ToolResult:
    """Unified tool response. Construct via the success/partial/error helpers."""

    status: str
    tool: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    error: dict[str, str] | None = None
    stats: dict[str, Any] | None = None

    @property
    def is_error(self) -> bool:
        return self.status == "error"

    @property
    def error_code(self) -> str | None:
        return self.error["code"] if self.error else None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict.

        Flattens `data` to the top level and aliases `text` as `summary` so
        existing consumers (frontend reads result.summary; Reflector reads
        result.total_papers / result.core_titles) keep working unchanged.
        """
        d: dict[str, Any] = {
            "status": self.status,
            "tool": self.tool,
            "text": self.text,
            "summary": self.text,
            "data": self.data,
        }
        if self.error is not None:
            d["error"] = self.error
        if self.stats is not None:
            d["stats"] = self.stats
        for k, v in self.data.items():
            d.setdefault(k, v)
        return d


def ok(tool: str, text: str = "", **data: Any) -> ToolResult:
    """Build a success ToolResult."""
    return ToolResult(status="success", tool=tool, text=text, data=data)


def partial_result(tool: str, text: str = "", **data: Any) -> ToolResult:
    """Build a partial-success ToolResult (e.g. search ran but few hits)."""
    return ToolResult(status="partial", tool=tool, text=text, data=data)


def err(tool: str, code: str, message: str) -> ToolResult:
    """Build an error ToolResult carrying a machine-readable error code."""
    return ToolResult(
        status="error", tool=tool, text=message,
        error={"code": code, "message": message},
    )
