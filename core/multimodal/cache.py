"""Vision result cache: in-memory LRU over SQLite (data/metadata.db vision_cache).

Keyed by (image_hash, task, prompt_version) — identical to the vision_cache
table schema. The LRU front avoids hitting SQLite for the common case
(re-analyzing the same image within one run); SQLite persists across runs so a
second deep_read of the same paper costs zero VLM tokens. prompt_version is part
of the key, so a prompt bump cleanly invalidates stale entries.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from typing import Any

from core.config import get_settings
from core.storage_context import StorageContext
from tools.storage.database import Database

logger = logging.getLogger(__name__)


class VisionCache:
    """Two-level cache: in-memory LRU + SQLite (vision_cache table).

    Every method swallows DB errors: the cache is best-effort and must never
    break the read pipeline. A cache miss or DB failure simply means the caller
    pays for a fresh VLM call.
    """

    def __init__(self, max_entries: int = 256,
                 storage_context: StorageContext | None = None):
        self._lru: "OrderedDict[str, Any]" = OrderedDict()
        self._max = max_entries
        self._storage_context = storage_context
        self._db: Database | None = None
        self._lock = threading.Lock()

    def _get_db(self) -> Database | None:
        # Lazily open the global metadata DB (same file as summary_cache).
        if self._db is None:
            try:
                self._db = Database(
                    storage_context=self._storage_context
                ) if self._storage_context is not None else Database(get_settings().storage.sqlite_path)
            except Exception as e:  # noqa: BLE001
                logger.debug("vision cache DB unavailable: %s", e)
                self._db = None
        return self._db

    @staticmethod
    def _key(image_hash: str, task: str, prompt_version: int) -> str:
        return f"{image_hash}::{task}::v{prompt_version}"

    def get(self, image_hash: str, task: str, prompt_version: int) -> Any | None:
        k = self._key(image_hash, task, prompt_version)
        with self._lock:
            hit = self._lru.get(k)
            if hit is not None:
                self._lru.move_to_end(k)
                return hit
        db = self._get_db()
        if db is None:
            return None
        try:
            raw = db.get_vision_cache(image_hash, task, prompt_version)
        except Exception as e:  # noqa: BLE001
            logger.debug("vision cache read failed: %s", e)
            return None
        if not raw:
            return None
        try:
            val = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
        with self._lock:
            self._lru[k] = val
            self._evict()
        return val

    def put(self, image_hash: str, task: str, prompt_version: int, result: Any) -> None:
        k = self._key(image_hash, task, prompt_version)
        with self._lock:
            self._lru[k] = result
            self._evict()
        db = self._get_db()
        if db is None:
            return
        try:
            db.save_vision_cache(
                image_hash, task, prompt_version,
                json.dumps(result, ensure_ascii=False),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("vision cache write failed: %s", e)

    def _evict(self) -> None:
        while len(self._lru) > self._max:
            self._lru.popitem(last=False)


_SINGLETONS: dict[str, VisionCache] = {}


def get_vision_cache(storage_context: StorageContext | None = None) -> VisionCache:
    """Return a cache partitioned by its metadata database path."""
    key = str(storage_context.metadata_db) if storage_context is not None else "web"
    cache = _SINGLETONS.get(key)
    if cache is None:
        cache = VisionCache(storage_context=storage_context)
        _SINGLETONS[key] = cache
    return cache
