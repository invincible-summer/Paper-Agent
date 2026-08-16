"""Backward-compatible facade over the refactored chat agent modules.

New code should import directly:
  - agents.orchestrator.chat_turn   (ReAct loop)
  - agents.session.ChatSession      (session state) + persistence helpers
  - agents.tools_impl               (tool implementations)

This module re-exports the public surface so existing imports
(`from agents.chat_agent import ...`) keep working.
"""
from __future__ import annotations

from agents.orchestrator import (  # noqa: F401
    _attachment_context,
    _build_tool_result_message,
    _lite_tool_calls,
    chat_turn,
    make_call_key,
    stream_scan,
)
from agents.session import (  # noqa: F401
    ChatSession,
    delete_chat_history,
    list_chat_history,
    load_chat_history,
    rename_chat_history,
    save_chat_history,
)
