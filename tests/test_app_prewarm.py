from __future__ import annotations

import asyncio

from core import prewarm


def test_prewarm_failure_is_recorded_not_raised(monkeypatch):
    def fail(_name):
        raise RuntimeError("optional import unavailable")
    monkeypatch.setattr(prewarm.importlib, "import_module", fail)
    state = prewarm.prewarm_agent_stack("blocking")
    assert state["prewarm_state"] == "failed"
    assert "optional import unavailable" in state["last_error"]


def test_prewarm_success_records_duration(monkeypatch):
    import core.llm as llm_module
    monkeypatch.setattr(llm_module, "get_llm", lambda _name: object())
    monkeypatch.setattr(prewarm.importlib, "import_module", lambda _name: object())
    state = prewarm.prewarm_agent_stack("background")
    assert state["prewarm_state"] == "ready"
    assert state["active_mode"] == "background"
    assert state["duration_ms"] >= 0


def test_lifespan_blocking_finishes_before_yield(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app import main as app_main
    from core.runtime_performance_policy import RuntimePerformancePolicy

    calls = []
    monkeypatch.setattr("core.runtime_performance_policy.get_performance_policy", lambda: RuntimePerformancePolicy(startup_prewarm_mode="blocking"))
    monkeypatch.setattr(app_main, "_retire_remote_fulltext_cache", lambda: {})
    monkeypatch.setattr(app_main, "_prewarm_agent_stack", lambda mode: calls.append(mode) or {})

    async def run():
        async with app_main.lifespan(app_main.app):
            assert calls == ["blocking"]
    asyncio.run(run())


def test_lifespan_background_yields_before_warmup_finishes(monkeypatch):
    import sys
    import threading
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app import main as app_main
    from core.runtime_performance_policy import RuntimePerformancePolicy

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def warm(_mode):
        started.set()
        release.wait(timeout=2)
        finished.set()
        return {}

    monkeypatch.setattr("core.runtime_performance_policy.get_performance_policy", lambda: RuntimePerformancePolicy(startup_prewarm_mode="background"))
    monkeypatch.setattr(app_main, "_retire_remote_fulltext_cache", lambda: {})
    monkeypatch.setattr(app_main, "_prewarm_agent_stack", warm)

    async def run():
        async with app_main.lifespan(app_main.app):
            assert not finished.is_set()
            release.set()
            for _ in range(100):
                if finished.is_set():
                    break
                await asyncio.sleep(0.01)
            assert finished.is_set()
    asyncio.run(run())
