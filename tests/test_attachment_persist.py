"""D-089: per-message attachments persist on session.messages and survive a
save→load round-trip so the chat UI can render file chips after reload.
"""
import asyncio
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_user_message_carries_turn_attachments(monkeypatch):
    import tempfile, os
    from types import SimpleNamespace
    import agents.orchestrator as orch
    from agents.chat_agent import ChatSession, chat_turn, save_chat_history, load_chat_history

    class _AnswerLLM:
        model = "test"
        model_name = "test"
        def bind_tools(self, tools, **kwargs):
            return self
        async def astream(self, messages, **kwargs):
            yield SimpleNamespace(content="收到", additional_kwargs={},
                                  tool_call_chunks=[], usage_metadata=None)

    monkeypatch.setattr(orch, "get_llm", lambda tier="light": _AnswerLLM())
    monkeypatch.setattr(orch, "_index_attachments", lambda session, attachments: None)

    tmpdir = tempfile.mkdtemp()
    sess = ChatSession()
    # Point the history store at a temp dir so we don't pollute real history.
    import core.history_store as hs
    orig_dir = hs.HISTORY_DIR if hasattr(hs, "HISTORY_DIR") else None
    hs.HISTORY_DIR = pathlib.Path(tmpdir)
    (pathlib.Path(tmpdir)).mkdir(parents=True, exist_ok=True)

    atts = [{"id": "f1", "filename": "paper.pdf", "char_count": 42},
            {"id": "f2", "filename": "notes.txt", "char_count": 7}]

    async def run():
        async for _ in chat_turn("你好", sess, attachments=atts):
            pass

    asyncio.run(run())

    # The last user message must carry BOTH attachments.
    user_msgs = [m for m in sess.messages if m["role"] == "user"]
    assert user_msgs, "expected a user message"
    last_user = user_msgs[-1]
    assert "attachments" in last_user, "user message missing attachments key"
    ids = [a["id"] for a in last_user["attachments"]]
    assert ids == ["f1", "f2"], f"expected [f1,f2], got {ids}"
    # Full text/bytes stay out of the per-message record; only lightweight
    # metadata needed by chips/image preview is persisted.
    allowed = {"id", "filename", "char_count", "ext", "media_type",
               "multimodal_status", "element_count", "preview_url"}
    assert all(set(a.keys()) <= allowed for a in last_user["attachments"])
    assert all("text_preview" not in a for a in last_user["attachments"])

    # Round-trip through save/load.
    fname = save_chat_history(sess)
    loaded = load_chat_history(fname)
    assert loaded is not None
    loaded_user = [m for m in loaded.messages if m["role"] == "user"][-1]
    assert [a["id"] for a in loaded_user["attachments"]] == ["f1", "f2"]
    # Top-level session.attachments also persisted (D-087).
    assert {a["id"] for a in loaded.attachments} == {"f1", "f2"}

    if orig_dir is not None:
        hs.HISTORY_DIR = orig_dir


if __name__ == "__main__":
    test_user_message_carries_turn_attachments()
    print("ok")
