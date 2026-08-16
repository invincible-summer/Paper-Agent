"""Prompt registry — single source of truth for all agent prompts.

Every prompt is a PromptDef(id, version, text) registered at import time.
Rules:
- Changing a prompt's text MUST bump its version (trace records the active
  versions each turn, so any answer can be traced back to the prompt text
  that produced it).
- Prompt modules (system.py / search.py / map_path.py) register their prompts
  here; callers use get(id).text instead of importing string constants.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptDef:
    id: str
    version: int
    text: str


_REGISTRY: dict[str, PromptDef] = {}


def register(prompt_id: str, version: int, text: str) -> PromptDef:
    """Register (or re-register with a bumped version) a prompt."""
    if not prompt_id or version < 1:
        raise ValueError("prompt_id required and version must be >= 1")
    p = PromptDef(id=prompt_id, version=version, text=text)
    _REGISTRY[prompt_id] = p
    return p


def get(prompt_id: str) -> PromptDef:
    """Fetch a registered prompt. KeyError lists available ids for debugging."""
    try:
        return _REGISTRY[prompt_id]
    except KeyError:
        raise KeyError(
            f"Unknown prompt id: {prompt_id!r}. "
            f"Registered: {sorted(_REGISTRY)}"
        ) from None


def active_versions() -> dict[str, int]:
    """Snapshot of id -> version, recorded into each turn's trace."""
    return {pid: p.version for pid, p in sorted(_REGISTRY.items())}
