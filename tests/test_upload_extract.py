"""Tests for the D-087 upload text extraction + chat_turn regenerate/attachments.

Covers the deterministic pure-function layer (_extract_text in the API module)
and the chat_turn contract for attachments/regenerate (session mutation only —
no LLM calls, using the chat_agent ChatSession directly).
"""
import sys, os, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

os.environ.setdefault("CHAT_NATIVE_FC", "1")


def test_extract_text_txt_utf8():
    from app.api.v1.chat import _extract_text
    raw = "这是一篇测试论文。".encode("utf-8")
    out = _extract_text("note.txt", raw)
    assert "测试论文" in out


def test_extract_text_md():
    from app.api.v1.chat import _extract_text
    raw = b"# Title\n\nsome markdown body"
    out = _extract_text("paper.md", raw)
    assert "some markdown body" in out


def test_extract_text_unsupported_returns_empty():
    from app.api.v1.chat import _extract_text
    out = _extract_text("image.png", b"\x89PNG")
    assert out == ""


def test_extract_text_docx():
    import io
    import docx
    from app.api.v1.chat import _extract_text

    doc = docx.Document()
    doc.add_paragraph("Introduction to graph neural networks.")
    doc.add_paragraph("Second paragraph with results.")
    buf = io.BytesIO()
    doc.save(buf)
    out = _extract_text("draft.docx", buf.getvalue())
    assert "graph neural networks" in out
    assert "Second paragraph" in out


def test_extract_text_tex_passthrough():
    from app.api.v1.chat import _extract_text
    raw = b"\\documentclass{article}\n\\begin{document}\nHello TeX.\n\\end{document}\n"
    out = _extract_text("paper.tex", raw)
    assert "Hello TeX" in out and "\\documentclass" in out


def test_extract_text_bib_passthrough():
    from app.api.v1.chat import _extract_text
    raw = b"@article{x, title = {T}, year = {2020}}"
    out = _extract_text("lib.bib", raw)
    assert "@article{x" in out


def test_doc_raises_guidance_to_save_as_docx():
    from tools.ingest.downloader import IngestError, extract_text
    import pytest
    with pytest.raises(IngestError) as ei:
        extract_text(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy-doc-binary", "doc")
    assert "另存为" in str(ei.value) and ".docx" in str(ei.value)


def test_attachment_context_reads_disk(tmp_path, monkeypatch):
    import agents.chat_agent as ca
    sess = ca.ChatSession()
    sess.attachments = [{"id": "abc", "filename": "p.txt", "char_count": 5}]
    # Point the helper's uploads dir at a temp location.
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "abc.txt").write_text("hello uploaded body", encoding="utf-8")
    import importlib
    # _attachment_context resolves data/uploads relative to the package; patch
    # by writing into the real data/uploads dir instead.
    real_dir = pathlib.Path(ca.__file__).resolve().parents[1] / "data" / "uploads"
    real_dir.mkdir(parents=True, exist_ok=True)
    (real_dir / "abc.txt").write_text("hello uploaded body", encoding="utf-8")
    ctx = ca._attachment_context(sess)
    assert "p.txt" in ctx
    assert "hello uploaded body" in ctx
    (real_dir / "abc.txt").unlink(missing_ok=True)


def test_attachment_context_empty_when_no_attachments():
    import agents.chat_agent as ca
    sess = ca.ChatSession()
    assert ca._attachment_context(sess) == ""


def test_regenerate_pops_last_assistant_message(monkeypatch):
    """chat_turn(regenerate=True) must drop the trailing assistant message and
    not re-append the user message. Verified via session state after a turn
    that short-circuits to the clarify-topic reply (no LLM call)."""
    import asyncio
    from types import SimpleNamespace
    import agents.orchestrator as orch
    from agents.chat_agent import ChatSession, chat_turn

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
    sess = ChatSession()
    sess.messages = [
        {"role": "user", "content": "搜论文"},
        {"role": "assistant", "content": "old reply", "thinking": "", "toolCalls": []},
    ]

    async def run():
        events = []
        async for ev in chat_turn("搜论文", sess, regenerate=True):
            events.append(ev)
        return events

    asyncio.run(run())
    # The old assistant reply must be gone (popped) and replaced by the new
    # clarify-topic reply; the user message must NOT be duplicated.
    assert sess.messages[0]["role"] == "user"
    assert sess.messages[0]["content"] == "搜论文"
    assert sess.messages[-1]["role"] == "assistant"
    assert sess.messages[-1]["content"] != "old reply"
    # No duplicate user message appended before the new assistant one.
    user_count = sum(1 for m in sess.messages if m["role"] == "user")
    assert user_count == 1, f"expected 1 user msg after regenerate, got {user_count}"


def test_attachments_dedup_and_persist(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import agents.orchestrator as orch
    from agents.chat_agent import ChatSession, chat_turn

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
    sess = ChatSession()
    atts = [{"id": "x1", "filename": "a.pdf", "char_count": 100},
            {"id": "x1", "filename": "a.pdf", "char_count": 100}]  # dup

    async def run():
        async for _ in chat_turn("你好", sess, attachments=atts):
            pass

    asyncio.run(run())
    # Deduped: only one x1 entry.
    ids = [a["id"] for a in sess.attachments]
    assert ids.count("x1") == 1
