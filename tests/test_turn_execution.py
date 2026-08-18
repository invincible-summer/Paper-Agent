"""Execution-budget and blocking-lane regression tests."""
from __future__ import annotations

import asyncio
import threading
import time

from agents import tools_impl
from agents.session import ChatSession
from core.blocking import run_cpu_bound, run_io_bound
from core.tool_protocol import ErrorCode, ok
from core.turn_execution import TurnExecutionContext


def test_io_lane_is_not_blocked_by_long_ml_job():
    started = threading.Event()
    release = threading.Event()

    def long_ml():
        started.set()
        release.wait(timeout=2)
        return "ml"

    async def main():
        ml_task = asyncio.create_task(run_cpu_bound(long_ml))
        while not started.is_set():
            await asyncio.sleep(0.01)
        try:
            result = await asyncio.wait_for(run_io_bound(lambda: "io"), timeout=0.5)
            assert result == "io"
        finally:
            release.set()
            await ml_task

    asyncio.run(main())


def test_cancelled_queued_ml_job_is_skipped():
    started = threading.Event()
    release = threading.Event()
    second_ran = threading.Event()

    def first():
        started.set()
        release.wait(timeout=2)

    def second():
        second_ran.set()

    async def main():
        first_task = asyncio.create_task(run_cpu_bound(first))
        while not started.is_set():
            await asyncio.sleep(0.01)
        second_task = asyncio.create_task(run_cpu_bound(second))
        await asyncio.sleep(0.02)
        second_task.cancel()
        try:
            await second_task
        except asyncio.CancelledError:
            pass
        release.set()
        await first_task
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert not second_ran.is_set()


def test_tool_timeout_uses_remaining_turn_budget(monkeypatch):
    async def slow(args, session, progress_cb):
        await asyncio.sleep(1)
        return ok("search_papers", "late")

    monkeypatch.setitem(tools_impl._IMPLS, "search_papers", slow)

    async def main():
        context = TurnExecutionContext.openai_api(
            soft_timeout_seconds=0.05, hard_timeout_seconds=0.1)
        started = time.monotonic()
        result = await tools_impl.execute_tool(
            {"name": "search_papers", "args": {"topic": "x"}},
            ChatSession(), execution_context=context,
        )
        assert time.monotonic() - started < 0.3
        return result

    result = asyncio.run(main())
    assert result.error_code == ErrorCode.TIMEOUT
