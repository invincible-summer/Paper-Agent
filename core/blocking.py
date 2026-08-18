"""Run synchronous local-ML/storage work without blocking FastAPI's event loop.

The web UI and the OpenAI-compatible channel share one Uvicorn event loop.
Docling, sentence-transformers, CrossEncoder, and Chroma expose synchronous
APIs; calling them directly from an ``async def`` stalls every HTTP/SSE route,
including the frontend's ``/api/v1/auth/config`` bootstrap request.

A single daemon worker is intentional for the supported 4C16G/single-Uvicorn
deployment: local ML jobs are serialized instead of competing for CPU/RAM.
Network LLM/VLM work remains on the normal async path.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar, cast

_T = TypeVar("_T")


@dataclass
class _Job(Generic[_T]):
    call: Callable[[], _T]
    done: threading.Event = field(default_factory=threading.Event)
    result: _T | None = None
    error: BaseException | None = None


_JOBS: queue.Queue[_Job[Any]] = queue.Queue()
_WORKER: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()


def _worker_main() -> None:
    global _WORKER
    while True:
        try:
            job = _JOBS.get_nowait()
        except queue.Empty:
            # Double-check under the lifecycle lock so a submitter cannot see
            # an alive worker, enqueue after our first check, and strand a job.
            with _WORKER_LOCK:
                try:
                    job = _JOBS.get_nowait()
                except queue.Empty:
                    _WORKER = None
                    return
        try:
            job.result = job.call()
        except BaseException as exc:  # propagate to the awaiting coroutine
            job.error = exc
        finally:
            job.done.set()
            _JOBS.task_done()


def _ensure_worker() -> None:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(
                target=_worker_main,
                name="paper-agent-local-ml",
                daemon=True,
            )
            _WORKER.start()


async def run_cpu_bound(
    func: Callable[..., _T], /, *args: Any, **kwargs: Any
) -> _T:
    """Run one blocking call on the process-wide single local-ML worker.

    Context variables are copied like ``asyncio.to_thread``. Cancellation stops
    waiting but cannot safely kill an already-running native parser/model call;
    the worker finishes it before accepting the next queued job.
    """
    context = contextvars.copy_context()
    call = functools.partial(func, *args, **kwargs)
    job: _Job[_T] = _Job(call=functools.partial(context.run, call))
    _JOBS.put(job)
    _ensure_worker()
    while not job.done.is_set():
        await asyncio.sleep(0.01)
    if job.error is not None:
        raise job.error
    return cast(_T, job.result)
