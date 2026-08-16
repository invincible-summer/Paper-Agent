"""Vision result cache tests (offline; temp SQLite DB).

Covers LRU get/put, task + prompt_version isolation, LRU eviction with DB
fallback, persistence across cache instances, and graceful handling of a
corrupt cached JSON.
"""

from __future__ import annotations

import json

from core.multimodal.cache import VisionCache
from tools.storage.database import Database


def _cache(tmp_path, max_entries: int = 4) -> VisionCache:
    """A VisionCache wired to a fresh temp DB (bypassing global settings)."""
    c = VisionCache(max_entries=max_entries)
    c._db = Database(str(tmp_path / "vision.db"))
    return c


def test_put_get_roundtrip(tmp_path):
    c = _cache(tmp_path)
    c.put("h1", "figure", 1, {"type": "architecture"})
    assert c.get("h1", "figure", 1) == {"type": "architecture"}


def test_task_and_version_isolation(tmp_path):
    c = _cache(tmp_path)
    c.put("h1", "figure", 1, {"a": 1})
    c.put("h1", "table", 1, {"b": 2})
    c.put("h1", "figure", 2, {"c": 3})
    assert c.get("h1", "figure", 1) == {"a": 1}
    assert c.get("h1", "table", 1) == {"b": 2}
    assert c.get("h1", "figure", 2) == {"c": 3}
    # Unknown combination is a miss, not an error.
    assert c.get("h1", "figure", 3) is None
    assert c.get("h2", "figure", 1) is None


def test_lru_eviction_falls_back_to_db(tmp_path):
    c = _cache(tmp_path, max_entries=4)
    for i in range(5):  # one beyond capacity
        c.put(f"h{i}", "figure", 1, {"i": i})
    # h0 was evicted from the in-memory LRU but persists in SQLite.
    assert c.get("h0", "figure", 1) == {"i": 0}


def test_persists_across_cache_instances(tmp_path):
    db_path = str(tmp_path / "vision.db")
    c1 = _cache(tmp_path)
    c1.put("hx", "figure", 1, {"k": "v"})
    # A second cache pointing at the same DB sees the persisted entry.
    c2 = VisionCache()
    c2._db = Database(db_path)
    assert c2.get("hx", "figure", 1) == {"k": "v"}


def test_corrupt_cache_json_returns_none(tmp_path):
    c = _cache(tmp_path)
    # Write a malformed JSON directly into the table.
    c._db.save_vision_cache("hbad", "figure", 1, "not-json{")
    assert c.get("hbad", "figure", 1) is None
