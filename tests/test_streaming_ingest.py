"""Remote API file ingestion streams to disk and validates every redirect."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import tools.ingest.downloader as downloader


class _Response:
    def __init__(self, status=200, chunks=None, headers=None):
        self.status_code = status
        self._chunks = list(chunks or [])
        self.headers = headers or {}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def aiter_bytes(self):
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


class _Client:
    responses = []
    requested = []
    def __init__(self, *args, **kwargs):
        self._responses = iter(type(self).responses)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def stream(self, method, url):
        type(self).requested.append(url)
        return next(self._responses)


def test_streams_chunks_to_private_temp_file(monkeypatch, tmp_path: Path):
    _Client.responses = [_Response(chunks=[b"abc", b"def"], headers={"content-type": "text/plain"})]
    _Client.requested = []
    monkeypatch.setattr(downloader.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(downloader, "_is_safe_url", lambda url: (True, ""))
    path, content_type = asyncio.run(downloader.download_to_temp(
        "https://public.example/file", temp_dir=tmp_path
    ))
    assert path.read_bytes() == b"abcdef"
    assert content_type == "text/plain"
    assert path.stat().st_mode & 0o777 == 0o600
    path.unlink()


def test_redirect_target_is_validated_before_second_request(monkeypatch, tmp_path: Path):
    _Client.responses = [_Response(status=302, headers={"location": "http://127.0.0.1/private"})]
    _Client.requested = []
    monkeypatch.setattr(downloader.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        downloader, "_is_safe_url",
        lambda url: (False, "private") if "127.0.0.1" in url else (True, ""),
    )
    with pytest.raises(downloader.IngestError, match="不安全"):
        asyncio.run(downloader.download_to_temp(
            "https://public.example/start", temp_dir=tmp_path
        ))
    assert _Client.requested == ["https://public.example/start"]
    assert list(tmp_path.iterdir()) == []


def test_interrupted_stream_removes_temp_file(monkeypatch, tmp_path: Path):
    _Client.responses = [_Response(chunks=[b"partial", RuntimeError("disconnect")])]
    _Client.requested = []
    monkeypatch.setattr(downloader.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(downloader, "_is_safe_url", lambda url: (True, ""))
    with pytest.raises(downloader.IngestError, match="下载出错"):
        asyncio.run(downloader.download_to_temp(
            "https://public.example/file", temp_dir=tmp_path
        ))
    assert list(tmp_path.iterdir()) == []


def test_configured_limit_is_reported_and_partial_file_is_removed(monkeypatch, tmp_path: Path):
    _Client.responses = [_Response(chunks=[b"a" * (4 * 1024 * 1024), b"b" * (3 * 1024 * 1024)])]
    _Client.requested = []
    monkeypatch.setattr(downloader.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(downloader, "_is_safe_url", lambda url: (True, ""))
    with pytest.raises(downloader.IngestError, match="6.0 MiB"):
        asyncio.run(downloader.download_to_temp(
            "https://public.example/file", temp_dir=tmp_path,
            max_bytes=6 * 1024 * 1024 + 1,
        ))
