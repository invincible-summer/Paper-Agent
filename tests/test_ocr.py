"""Stage 1.5 scanned-page recovery tests (offline; fitz + vision stubbed).

recover_scanned_pages() is the VLM-OCR fallback for PDFs Docling's OCR could not
recover. These tests pin its industrial-grade properties: graceful no-op when
vision is unconfigured, budget-bounded page count, partial-failure isolation,
and concatenation of successful pages.
"""

from __future__ import annotations

import asyncio

import fitz

import core.multimodal.ocr as ocr


class FakeClient:
    """Records analyze() calls; returns a fixed OCR string, optionally failing."""

    def __init__(self, result="recovered text", fail_pages=None):
        self.calls: list[bytes] = []
        self._result = result
        self._fail_pages = fail_pages or set()  # 0-based page indices that raise

    async def analyze(self, png, task, prompt, *, prompt_version=1, expect_json=True, mime="image/png"):
        # Fail deterministically by png byte identity if requested.
        self.calls.append(png)
        if len(self.calls) - 1 in self._fail_pages:
            raise RuntimeError("ocr boom")
        return self._result


class _Pix:
    def tobytes(self, fmt):  # noqa: ARG002
        return b"png-bytes"


class _Page:
    def get_pixmap(self, matrix):  # noqa: ARG002
        return _Pix()


class _Doc:
    def __init__(self, n):
        self.page_count = n

    def __getitem__(self, i):
        return _Page()

    def close(self):
        pass


def _patch_fitz(monkeypatch, n_pages):
    monkeypatch.setattr(fitz, "open", lambda p: _Doc(n_pages))  # noqa: ARG005


def _patch(monkeypatch, client=None, cap=10):
    monkeypatch.setattr(ocr, "get_vision_client", lambda: client)
    monkeypatch.setattr(ocr, "SCAN_VLM_PAGES_PER_PAPER", cap)


def test_vision_unconfigured_returns_empty(monkeypatch, tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF fake")
    _patch_fitx = None  # not even reached: client is None short-circuits
    _patch(monkeypatch, client=None)
    out = asyncio.run(ocr.recover_scanned_pages(str(pdf)))
    assert out == ""


def test_recovers_all_pages_when_under_cap(monkeypatch, tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF fake")
    _patch_fitz(monkeypatch, n_pages=3)
    client = FakeClient(result="page text")
    _patch(monkeypatch, client=client, cap=10)
    out = asyncio.run(ocr.recover_scanned_pages(str(pdf)))
    assert out.count("page text") == 3
    assert len(client.calls) == 3


def test_page_cap_bounds_vlm_calls(monkeypatch, tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF fake")
    _patch_fitz(monkeypatch, n_pages=8)
    client = FakeClient(result="t")
    _patch(monkeypatch, client=client, cap=2)
    asyncio.run(ocr.recover_scanned_pages(str(pdf)))
    assert len(client.calls) == 2  # SCAN_VLM_PAGES_PER_PAPER wins over page_count


def test_partial_failure_isolates_pages(monkeypatch, tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF fake")
    _patch_fitz(monkeypatch, n_pages=3)
    client = FakeClient(result="ok", fail_pages={1})  # page 1 (0-based) raises
    _patch(monkeypatch, client=client, cap=10)
    out = asyncio.run(ocr.recover_scanned_pages(str(pdf)))
    assert out.count("ok") == 2  # pages 0 and 2 still contributed
    assert len(client.calls) == 3


def test_fitz_open_failure_returns_empty(monkeypatch, tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF fake")

    def _boom(_):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(fitz, "open", _boom)
    client = FakeClient()
    _patch(monkeypatch, client=client, cap=10)
    out = asyncio.run(ocr.recover_scanned_pages(str(pdf)))
    assert out == ""
    assert client.calls == []  # never reached the VLM


def test_empty_result_per_page_is_dropped(monkeypatch, tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF fake")
    _patch_fitz(monkeypatch, n_pages=2)
    client = FakeClient(result="")  # VLM returns empty for every page
    _patch(monkeypatch, client=client, cap=10)
    out = asyncio.run(ocr.recover_scanned_pages(str(pdf)))
    assert out == ""  # all-empty -> nothing to concatenate
