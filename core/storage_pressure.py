"""Disk-pressure admission control for API-only heavy operations."""
from __future__ import annotations

import shutil
import time
import uuid
from contextlib import asynccontextmanager

from core.api_storage_store import ApiStorageStore
from core.storage_context import StorageContext

HEAVY_OPERATIONS = {"upload", "download", "deep_read", "ocr", "vision", "export"}
ALL_OPERATIONS = HEAVY_OPERATIONS | {"text_chat"}


class StoragePressureError(RuntimeError):
    def __init__(self, operation: str, disk_percent: float, threshold: int, reason: str):
        super().__init__(reason)
        self.operation = operation
        self.disk_percent = disk_percent
        self.threshold = threshold
        self.reason = reason


class StoragePressureGuard:
    def __init__(self, context: StorageContext, *, disk_percent=None):
        self.context = context
        self.storage = ApiStorageStore(context)
        self.storage.initialize()
        self._disk_percent = disk_percent or self._real_disk_percent

    def _real_disk_percent(self) -> float:
        usage = shutil.disk_usage(self.context.root_dir)
        return (usage.used / usage.total) * 100 if usage.total else 0.0

    def evaluate(self, operation: str) -> dict:
        if operation not in ALL_OPERATIONS:
            raise ValueError(f"unknown storage operation: {operation}")
        percent = float(self._disk_percent())
        policy = self.storage.get_policy()
        reason = "ok"
        threshold = policy.critical_threshold_percent
        heavy_paused = False
        if percent >= policy.hard_stop_threshold_percent:
            heavy_paused = True
            threshold = policy.hard_stop_threshold_percent
            reason = "hard_stop"
        elif percent >= policy.critical_threshold_percent and policy.critical_strategy == "pause_heavy":
            heavy_paused = True
            threshold = policy.critical_threshold_percent
            reason = "critical_pause_heavy"
        allowed = operation == "text_chat" or not heavy_paused
        paused = int(heavy_paused)
        with self.storage.connect() as conn:
            conn.execute(
                "UPDATE api_runtime_state SET heavy_writes_paused=?, pause_reason=?, disk_percent=?, updated_at=? WHERE id=1",
                (paused, reason if paused else None, percent, time.time()),
            )
            conn.commit()
        return {"allowed": allowed, "disk_percent": percent, "reason": reason,
                "threshold": threshold, "operation": operation}

    def ensure_allowed(self, operation: str) -> dict:
        outcome = self.evaluate(operation)
        if not outcome["allowed"]:
            raise StoragePressureError(
                operation, outcome["disk_percent"], outcome["threshold"], outcome["reason"]
            )
        return outcome

    @asynccontextmanager
    async def protect(self, operation: str, session_id: str, *, ttl_seconds: int = 2 * 3600):
        self.ensure_allowed(operation)
        op_id = uuid.uuid4().hex
        now = time.time()
        with self.storage.connect() as conn:
            conn.execute(
                "INSERT INTO api_inflight_operations(id, session_id, operation, started_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (op_id, session_id, operation, now, now + ttl_seconds),
            )
            conn.commit()
        try:
            yield
        finally:
            with self.storage.connect() as conn:
                conn.execute("DELETE FROM api_inflight_operations WHERE id = ?", (op_id,))
                conn.commit()


def guard_for_session(session):
    context = getattr(session, "storage_context", None)
    if context is None or context.channel != "openai_api":
        return None
    return StoragePressureGuard(context)
