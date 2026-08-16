"""D-089: a file uploaded in turn 1 stays available to turn 2 even when turn 2
carries no new attachments. The agent must still see the uploaded file in its
context so the user can re-discuss it.
"""
import asyncio, os, pathlib, tempfile, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("CHAT_NATIVE_FC", "1")


def test_file_available_in_later_turn_without_reupload(tmp_path=None):
    import agents.chat_agent as ca
    sess = ca.ChatSession()
    # Simulate a real on-disk upload so _attachment_context can read it back.
    uploads = pathlib.Path(ca.__file__).resolve().parents[1] / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    aid = "revisit-test-1"
    (uploads / f"{aid}.txt").write_text(
        "Uploaded paper: Attention Is All You Need. Key idea: self-attention replaces recurrence.",
        encoding="utf-8",
    )
    sess.attachments = [{"id": aid, "filename": "attention.pdf", "char_count": 90}]

    # Turn 2: a follow-up question with NO new attachments — the user just
    # asks about the file uploaded earlier.
    ctx = ca._attachment_context(sess)
    try:
        assert "attention.pdf" in ctx, "later turn did not see the earlier file"
        assert "Attention Is All You Need" in ctx, "file content not injected in later turn"
        print("PASS: earlier file visible to a later turn with no re-upload")
    finally:
        (uploads / f"{aid}.txt").unlink(missing_ok=True)


if __name__ == "__main__":
    test_file_available_in_later_turn_without_reupload()
