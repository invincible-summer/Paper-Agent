"""Schemas for chat API."""
from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    history_filename: str | None = None
    # Session state (restored from history)
    topic: str = ""
    conception: str = ""
    language: str = "both"
    field_profile: str = "general"
    # Attachments uploaded via the chat input (paperclip). Each is
    # {id, filename, char_count} — the full extracted text lives server-side
    # at data/uploads/<id>.txt.
    attachments: list[dict] = []
    # Regenerate the last assistant response: pops the last assistant message
    # and re-runs the turn without re-appending the user message.
    regenerate: bool = False


class ChatHistoryItem(BaseModel):
    filename: str
    timestamp: str
    topic: str
    message_count: int = 0
    title: str = ""
    paper_count: int = 0
    has_review: bool = False
    has_map: bool = False
    trace_ids: list[str] = []
    source: str = "chat"


class ChatHistoryListResponse(BaseModel):
    records: list[ChatHistoryItem]


class ChatRenameRequest(BaseModel):
    title: str
