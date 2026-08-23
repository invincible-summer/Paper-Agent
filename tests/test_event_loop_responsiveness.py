"""Regression tests for /v1 local-ML work blocking the web API event loop."""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agents import reader_agent as ra
from app.main import create_app
from core.blocking import run_cpu_bound
from core.models import Paper
from tools.pdf.structure.models import ParsedPaperDocument
from tools.storage.database import Database


def test_run_cpu_bound_keeps_event_loop_responsive():
    started = threading.Event()
    release = threading.Event()

    def slow_job() -> str:
        started.set()
        release.wait(timeout=2)
        return "done"

    async def scenario():
        task = asyncio.create_task(run_cpu_bound(slow_job))
        while not started.is_set():
            await asyncio.sleep(0.005)
        # This sleep represents unrelated FastAPI/SSE work on the shared loop.
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
        assert not task.done()
        release.set()
        assert await asyncio.wait_for(task, timeout=1) == "done"

    asyncio.run(scenario())


def test_local_ml_jobs_are_process_wide_serialized():
    lock = threading.Lock()
    active = 0
    max_active = 0

    def job() -> None:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.04)
        with lock:
            active -= 1

    async def scenario():
        await asyncio.gather(run_cpu_bound(job), run_cpu_bound(job))

    asyncio.run(scenario())
    assert max_active == 1


def test_auth_bootstrap_responds_while_pdf_parser_is_busy(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class SlowParser:
        def parse(self, *_args, **_kwargs):
            started.set()
            release.wait(timeout=2)
            return ParsedPaperDocument(raw_text="digital text", is_scanned=False)

    monkeypatch.setattr(ra, "get_structure_parser", lambda: SlowParser())
    paper = Paper(id="upload:P1", title="Slow PDF", source="upload",
                  pdf_path="/fake/slow.pdf")

    async def scenario():
        parse_task = asyncio.create_task(
            ra.parse_and_understand(paper, Database(":memory:"))
        )
        while not started.is_set():
            await asyncio.sleep(0.005)

        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await asyncio.wait_for(
                client.get("/api/v1/auth/config"), timeout=0.25
            )
        assert response.status_code == 200
        assert "auth_required" in response.json()
        assert not parse_task.done()

        release.set()
        parsed = await asyncio.wait_for(parse_task, timeout=1)
        assert parsed is not None
        assert parsed.raw_text == "digital text"

    asyncio.run(scenario())


def test_worker_propagates_errors_and_recovers():
    def fail() -> None:
        raise RuntimeError("boom")

    async def scenario():
        try:
            await run_cpu_bound(fail)
        except RuntimeError as exc:
            assert str(exc) == "boom"
        else:  # pragma: no cover - explicit assertion branch
            raise AssertionError("worker error was not propagated")
        assert await run_cpu_bound(lambda: 7) == 7

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_poison_worker():
    started = threading.Event()
    release = threading.Event()

    def slow_job() -> None:
        started.set()
        release.wait(timeout=2)

    async def scenario():
        task = asyncio.create_task(run_cpu_bound(slow_job))
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        release.set()
        # The native job finishes in the background; the next queued job runs.
        assert await asyncio.wait_for(run_cpu_bound(lambda: "ok"), timeout=1) == "ok"

    asyncio.run(scenario())
