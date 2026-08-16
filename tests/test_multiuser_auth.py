"""Multi-user auth: user store, history isolation, conversation-key salt."""
import time

import pytest

import core.user_store as us


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "_DB_PATH", tmp_path / "users.db")
    return us


def test_register_and_authenticate(store):
    user = store.create_user("alice", "password123", "Alice")
    assert user["username"] == "alice" and user["display_name"] == "Alice"
    assert store.authenticate("alice", "password123")["id"] == user["id"]
    assert store.authenticate("alice", "wrong-password") is None
    assert store.authenticate("nobody", "password123") is None
    # case-insensitive username lookup
    assert store.authenticate("ALICE", "password123") is not None


def test_duplicate_username_rejected(store):
    store.create_user("bob", "password123")
    with pytest.raises(store.UserStoreError):
        store.create_user("Bob", "password456")


def test_validation(store):
    with pytest.raises(store.UserStoreError):
        store.create_user("x", "password123")          # username too short
    with pytest.raises(store.UserStoreError):
        store.create_user("carol", "short")            # password too short
    with pytest.raises(store.UserStoreError):
        store.create_user("-bad", "password123")       # leading symbol


def test_token_lifecycle(store):
    user = store.create_user("dave", "password123")
    token = store.issue_token(user["id"])
    assert store.resolve_token(token)["username"] == "dave"
    store.revoke_token(token)
    assert store.resolve_token(token) is None
    assert store.resolve_token("garbage-token") is None


def test_token_expiry(store, monkeypatch):
    user = store.create_user("erin", "password123")
    token = store.issue_token(user["id"])
    # Fast-forward past the TTL by rewriting expires_at.
    import sqlite3
    conn = sqlite3.connect(str(store._DB_PATH))
    conn.execute("UPDATE tokens SET expires_at = ?", (time.time() - 1,))
    conn.commit(); conn.close()
    assert store.resolve_token(token) is None


# --- history isolation -------------------------------------------------------

@pytest.fixture()
def hist(tmp_path, monkeypatch):
    import core.history_store as hs
    monkeypatch.setattr(hs, "HISTORY_DIR", tmp_path / "history")
    return hs


def test_history_isolated_per_user(hist):
    f_a = hist.save_session({"messages": [{"role": "user", "content": "A 的秘密"}]},
                            user_id="ua")
    f_b = hist.save_session({"messages": [{"role": "user", "content": "B 的内容"}]},
                            user_id="ub")
    assert f_a != f_b
    assert [r["filename"] for r in hist.list_sessions(user_id="ua")] == [f_a]
    assert [r["filename"] for r in hist.list_sessions(user_id="ub")] == [f_b]
    # cross-account access reads as not-found
    assert hist.load_session(f_a, user_id="ub") is None
    assert hist.rename_session(f_a, "hijack", user_id="ub") is None
    assert hist.delete_session(f_a, user_id="ub") is False
    assert hist.load_session(f_a, user_id="ua") is not None
    assert hist.delete_session(f_a, user_id="ua") is True


def test_save_never_overwrites_other_users_record(hist):
    f = hist.save_session({"messages": [{"role": "user", "content": "owner"}]}, user_id="ua")
    f2 = hist.save_session({"messages": [{"role": "user", "content": "attacker"}]},
                           filename=f, user_id="ub")
    assert f2 != f  # forked to a fresh file
    assert hist.load_session(f, user_id="ua")["messages"][0]["content"] == "owner"


def test_legacy_records_belong_to_local(hist):
    f = hist.save_session({"messages": [{"role": "user", "content": "old"}]})
    # strip user_id to simulate a pre-auth record
    import json
    fp = hist.HISTORY_DIR / f
    data = json.loads(fp.read_text(encoding="utf-8"))
    data.pop("user_id", None)
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert hist.load_session(f, user_id="local") is not None
    assert hist.load_session(f, user_id="someone") is None


# --- conversation key salt ---------------------------------------------------

def test_conversation_key_salt():
    from core.session_memory import conversation_key
    msgs = [{"role": "user", "content": "图神经网络推荐系统"}]
    k1 = conversation_key(msgs)
    k2 = conversation_key(msgs, salt="user-1")
    k3 = conversation_key(msgs, salt="user-2")
    assert k1 != k2 and k2 != k3
    assert conversation_key(msgs, salt="user-1") == k2  # stable
