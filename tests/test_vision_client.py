"""Vision client tests (offline; ChatOpenAI call stubbed).

Covers message construction, lenient JSON parsing, the JSON path with caching,
the OCR raw-text path, graceful None on call failure, breaker-open
short-circuit, and get_vision_client() returning None when unconfigured.
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage

from core.circuit_breaker import CircuitBreaker
from core.config import MultimodalConfig, Settings
from core.multimodal.cache import VisionCache
from core.multimodal import vision_client as vc
from core.multimodal.vision_client import (
    VisionClient,
    _parse_json_lenient,
    get_vision_client,
)
from tools.storage.database import Database


def _client(tmp_path) -> VisionClient:
    """A VisionClient with a temp cache + fresh breaker (no global state)."""
    c = VisionClient(
        MultimodalConfig(api_key="k", base_url="http://x/v1", model="m")
    )
    c._cache = VisionCache()
    c._cache._db = Database(str(tmp_path / "vision.db"))
    c._breaker = CircuitBreaker()
    return c


# --- pure helpers --------------------------------------------------------------

def test_build_messages_structure():
    msgs = VisionClient._build_messages("hello", b"\x89PNGfake")
    assert len(msgs) == 1 and isinstance(msgs[0], HumanMessage)
    assert isinstance(msgs[0].content, list)
    parts = {p["type"]: p for p in msgs[0].content}
    assert parts["text"]["text"] == "hello"
    url = parts["image_url"]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_parse_json_lenient_variants():
    assert _parse_json_lenient('{"a": 1}') == {"a": 1}
    assert _parse_json_lenient("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert _parse_json_lenient('prose before {"a": 1} trailing') == {"a": 1}
    assert _parse_json_lenient("not json at all") is None
    assert _parse_json_lenient("") is None
    # A JSON array is not an object -> None (callers expect a dict).
    assert _parse_json_lenient("[1, 2, 3]") is None


# --- analyze paths -------------------------------------------------------------

def test_analyze_json_parses_and_caches(tmp_path):
    c = _client(tmp_path)

    async def fake_call(messages, max_tokens):
        return '{"type":"architecture","description":"d","components":[],"relations":[],"role_in_paper":"r"}'

    c._call_with_retry = fake_call  # bypass tenacity for determinism

    out = asyncio.run(c.analyze(b"img", "figure", "prompt", prompt_version=1))
    assert out == {"type": "architecture", "description": "d",
                   "components": [], "relations": [], "role_in_paper": "r"}

    # Second call must hit the cache even if the LLM would now fail.
    async def failing(messages, max_tokens):
        raise RuntimeError("should not be called")

    c._call_with_retry = failing
    out2 = asyncio.run(c.analyze(b"img", "figure", "prompt", prompt_version=1))
    assert out2 == out


def test_analyze_returns_none_on_failure(tmp_path):
    c = _client(tmp_path)

    async def boom(messages, max_tokens):
        raise RuntimeError("network gone")

    c._call_with_retry = boom
    assert asyncio.run(c.analyze(b"img", "figure", "p")) is None


def test_analyze_ocr_returns_raw_text(tmp_path):
    c = _client(tmp_path)

    async def fake_call(messages, max_tokens):
        return "Some transcribed page text."

    c._call_with_retry = fake_call
    out = asyncio.run(
        c.analyze(b"page", "ocr", "ocr prompt", expect_json=False)
    )
    assert out == "Some transcribed page text."


def test_analyze_returns_none_when_breaker_open(tmp_path):
    c = _client(tmp_path)
    opened = CircuitBreaker(threshold=1, cooldown_s=60)
    opened.record_failure(vc._BREAKER_KEY)  # now open
    c._breaker = opened

    called = {"n": 0}

    async def fake_call(messages, max_tokens):
        called["n"] += 1
        return "{}"

    c._call_with_retry = fake_call
    assert asyncio.run(c.analyze(b"img", "figure", "p")) is None
    assert called["n"] == 0  # breaker short-circuited before any call


def test_analyze_empty_image_returns_none(tmp_path):
    c = _client(tmp_path)
    assert asyncio.run(c.analyze(b"", "figure", "p")) is None


# --- singleton gating ----------------------------------------------------------

def test_get_vision_client_none_when_unconfigured(monkeypatch):
    vc._CLIENT = None
    s = Settings()
    s.multimodal = MultimodalConfig()  # all fields empty
    monkeypatch.setattr(vc, "get_settings", lambda: s)
    assert get_vision_client() is None
