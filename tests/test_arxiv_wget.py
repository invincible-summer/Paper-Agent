from __future__ import annotations

import asyncio
from pathlib import Path

from tools.pdf import arxiv_wget


class Process:
    def __init__(self, path: Path, body: bytes, returncode: int = 0):
        path.write_bytes(body); self.returncode = returncode
    async def wait(self): return self.returncode
    def terminate(self): self.returncode = -15
    def kill(self): self.returncode = -9


def test_wget_uses_argv_lynx_and_no_shell(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_wget, "_validate_arxiv_url", lambda _url: (True, ""))
    captured = {}
    async def create(*args, **kwargs):
        captured["args"] = args; captured["kwargs"] = kwargs
        path = Path(args[args.index("-O") + 1])
        return Process(path, b"%PDF-1.7 sample")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    result = asyncio.run(arxiv_wget.fetch_arxiv_pdf(
        "https://arxiv.org/pdf/1706.03762.pdf", max_bytes=1024, timeout=5, probe=True))
    assert result.ok is True
    assert "--user-agent=Lynx" in captured["args"]
    assert any(str(arg).startswith("--header=Range:") for arg in captured["args"])
    assert "shell" not in captured["kwargs"]


def test_wget_rejects_unsafe_before_process(monkeypatch):
    called = False
    async def create(*_args, **_kwargs):
        nonlocal called; called = True
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    result = asyncio.run(arxiv_wget.fetch_arxiv_pdf(
        "http://127.0.0.1/pdf/x.pdf", max_bytes=10, timeout=1))
    assert result.error_code == "arxiv_url_not_allowed"
    assert called is False


def test_wget_non_pdf_and_temp_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_wget, "_validate_arxiv_url", lambda _url: (True, ""))
    created = None
    async def create(*args, **_kwargs):
        nonlocal created
        created = Path(args[args.index("-O") + 1])
        return Process(created, b"<html>")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    result = asyncio.run(arxiv_wget.fetch_arxiv_pdf(
        "https://arxiv.org/pdf/x.pdf", max_bytes=100, timeout=1))
    assert result.error_code == "not_pdf"
    assert created is not None and not created.exists()


def test_wget_file_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_wget, "_validate_arxiv_url", lambda _url: (True, ""))
    async def create(*args, **_kwargs):
        path = Path(args[args.index("-O") + 1])
        return Process(path, b"%PDF-" + b"x" * 100)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    result = asyncio.run(arxiv_wget.fetch_arxiv_pdf(
        "https://arxiv.org/pdf/x.pdf", max_bytes=10, timeout=1))
    assert result.error_code == "file_too_large"


def test_wget_rejects_traversal_and_query_before_process(monkeypatch):
    called = False

    async def create(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    for url in (
        "https://arxiv.org/pdf/../../etc/passwd.pdf",
        "https://arxiv.org/pdf/%2e%2e/etc/passwd.pdf",
        "https://arxiv.org/pdf/1706.03762.pdf?output=1",
    ):
        result = asyncio.run(
            arxiv_wget.fetch_arxiv_pdf(url, max_bytes=1024, timeout=1)
        )
        assert result.error_code == "arxiv_pdf_path_invalid"
    assert called is False


def test_wget_nonzero_exit_is_reported(monkeypatch):
    monkeypatch.setattr(arxiv_wget, "_validate_arxiv_url", lambda _url: (True, ""))

    async def create(*args, **_kwargs):
        path = Path(args[args.index("-O") + 1])
        return Process(path, b"", returncode=8)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    result = asyncio.run(
        arxiv_wget.fetch_arxiv_pdf(
            "https://arxiv.org/pdf/1706.03762.pdf", max_bytes=1024, timeout=1
        )
    )
    assert result.error_code == "wget_exit_nonzero"
    assert result.exit_code == 8


def test_wget_full_download_moves_verified_pdf_to_destination(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_wget, "_validate_arxiv_url", lambda _url: (True, ""))
    captured = {}

    async def create(*args, **_kwargs):
        captured["args"] = args
        path = Path(args[args.index("-O") + 1])
        return Process(path, b"%PDF-1.7 full body")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    destination = tmp_path / "paper.pdf"
    result = asyncio.run(
        arxiv_wget.fetch_arxiv_pdf(
            "https://arxiv.org/pdf/1706.03762.pdf",
            max_bytes=1024,
            timeout=1,
            destination=destination,
        )
    )
    assert result.ok is True
    assert result.path == str(destination)
    assert destination.read_bytes().startswith(b"%PDF-")
    assert not any(str(arg).startswith("--header=Range:") for arg in captured["args"])


def test_wget_timeout_terminates_process_and_cleans_temp(monkeypatch):
    monkeypatch.setattr(arxiv_wget, "_validate_arxiv_url", lambda _url: (True, ""))
    monkeypatch.setattr(arxiv_wget, "_PROCESS_POLL_SECONDS", 0.001)
    created = None

    class HangingProcess:
        def __init__(self, path: Path):
            self.returncode = None
            self.terminated = False
            self.killed = False
            path.write_bytes(b"%PDF-")

        async def wait(self):
            while self.returncode is None:
                await asyncio.sleep(0.001)
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = None

    async def create(*args, **_kwargs):
        nonlocal created, process
        created = Path(args[args.index("-O") + 1])
        process = HangingProcess(created)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    # The adapter clamps timeouts to one second; stub loop time so the bounded
    # timeout path can be tested without a real one-second sleep.
    times = iter((0.0, 2.0, 2.0, 2.0))
    real_get_running_loop = asyncio.get_running_loop

    class LoopProxy:
        def __init__(self, loop):
            self._loop = loop

        def time(self):
            return next(times, 2.0)

        def __getattr__(self, name):
            return getattr(self._loop, name)

    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: LoopProxy(real_get_running_loop()),
    )
    result = asyncio.run(
        arxiv_wget.fetch_arxiv_pdf(
            "https://arxiv.org/pdf/1706.03762.pdf", max_bytes=1024, timeout=1
        )
    )
    assert result.error_code == "timeout"
    assert process is not None and process.terminated is True
    assert created is not None and not created.exists()


def test_timeout_escalates_from_terminate_to_kill(monkeypatch):
    class ProcessThatIgnoresTerminate:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.killed = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            if self.returncode is None:
                await asyncio.sleep(3600)
            return self.returncode

    async def timeout_once(_awaitable, timeout):
        assert timeout == 1.0
        if hasattr(_awaitable, "close"):
            _awaitable.close()
        raise asyncio.TimeoutError

    process = ProcessThatIgnoresTerminate()
    monkeypatch.setattr(arxiv_wget.asyncio, "wait_for", timeout_once)
    asyncio.run(arxiv_wget._stop_process(process))
    assert process.terminated is True
    assert process.killed is True
    assert process.returncode == -9


def test_probe_diagnostic_and_real_download_share_adapter(monkeypatch, tmp_path):
    from tools.pdf.fetcher import PDFFetcher, probe_pdf_url
    from tools.search import diagnostics

    calls = []

    async def fake_fetch(url, **kwargs):
        calls.append((url, kwargs))
        destination = kwargs.get("destination")
        if destination:
            Path(destination).write_bytes(b"%PDF-1.7")
        return arxiv_wget.ArxivWgetResult(
            True, "ok", None, 10, 8192, 800.0, 0, True,
            str(destination) if destination else None,
        )

    monkeypatch.setattr(arxiv_wget, "fetch_arxiv_pdf", fake_fetch)
    url = "https://arxiv.org/pdf/1706.03762.pdf"
    assert asyncio.run(probe_pdf_url(url)) == (True, "ok")
    speed = asyncio.run(diagnostics._speed_test_url("arxiv", url))
    assert speed["status"] == "ok"
    # An arXiv URL discovered through OpenAlex/S2 still uses the same wget
    # adapter; dispatch is based on the final URL host, not the metadata source.
    discovered_speed = asyncio.run(diagnostics._speed_test_url("openalex", url))
    assert discovered_speed["status"] == "ok"

    fetcher = PDFFetcher.__new__(PDFFetcher)
    fetcher.storage_context = None
    destination = tmp_path / "paper.pdf"
    assert asyncio.run(fetcher._download(url, destination)) == str(destination)

    assert len(calls) == 4
    assert calls[0][1]["probe"] is True
    assert calls[1][1]["probe"] is True
    assert calls[2][1]["probe"] is True
    assert calls[3][1]["destination"] == destination
