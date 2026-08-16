"""OpenAI-compatible vision client for paper-element understanding.

Wraps langchain_openai.ChatOpenAI pointed at MULTIMODAL_BASE_URL/MODEL and sends
list-form content blocks (text + image_url data-URI). Deliberately independent
from the text LLM (core/llm.py / DeepSeek): separate config, separate
concurrency semaphore, separate cache. The text pipeline is never touched.

Resilience (industrial-grade, no shortcuts):
- Circuit breaker (core/circuit_breaker.py): a run of failures opens the
  provider so we stop spending tokens against a dead endpoint; calls return
  None and callers degrade to text/caption-only.
- Per-call tenacity backoff on transient errors (network / timeout / 429 / 5xx);
  non-retryable errors (400 / 401) fail fast and do not trip the breaker.
- Bounded concurrency via asyncio.Semaphore (default 2).
- image-hash cache (core/multimodal/cache.py) so a second read costs zero tokens.

Every public entry returns None on any failure — callers MUST handle None by
falling back to text/caption-only behavior. Vision is an enhancement, never a
hard dependency.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.circuit_breaker import get_breaker
from core.config import MultimodalConfig, get_settings
from core.multimodal import prompt_templates  # noqa: F401  (registers vision.* prompts)
from core.multimodal.cache import get_vision_cache

logger = logging.getLogger(__name__)

_BREAKER_KEY = "multimodal.vision"

# Transient errors worth retrying with backoff before giving up. The OpenAI
# client also has its own max_retries at the HTTP layer; this is a second,
# explicit layer that also covers connection/timeout surfaces uniformly.
_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _strip_json_fence(text: str) -> str:
    """Strip a single surrounding ```json ... ``` fence if present."""
    m = _JSON_FENCE_RE.match(text.strip())
    return m.group(1) if m else text.strip()


def _parse_json_lenient(text: str) -> dict | None:
    """Parse a JSON object from model text, tolerating code fences / prose.

    Returns None on any failure rather than raising — a non-JSON VLM reply is a
    soft failure handled by the caller (degrade to caption-only).
    """
    if not text:
        return None
    cleaned = _strip_json_fence(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        obj = json.loads(cleaned)
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


class VisionClient:
    """Async vision client. Build via get_vision_client(); direct construction
    requires a fully populated MultimodalConfig (api_key + base_url + model)."""

    def __init__(self, cfg: MultimodalConfig):
        self._cfg = cfg
        self._llm = ChatOpenAI(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            temperature=cfg.temperature,
            max_retries=cfg.max_retries,
            timeout=cfg.timeout,
            stream_usage=True,  # surface image-token usage via usage_metadata
        )
        self._sem = asyncio.Semaphore(max(1, cfg.max_concurrent))
        self._cache = get_vision_cache()
        self._breaker = get_breaker()

    # -- message construction (kept separate for unit tests) --
    @staticmethod
    def _build_messages(
        prompt: str, image_bytes: bytes, mime: str = "image/png"
    ) -> list[HumanMessage]:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_uri = f"data:{mime};base64,{b64}"
        return [
            HumanMessage(
                content=[
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ]
            )
        ]

    async def _call_llm(self, messages: list[HumanMessage], max_tokens: int) -> str:
        """Invoke the underlying model. Isolated so tests can monkeypatch."""
        bound = self._llm.bind(max_tokens=max_tokens)
        resp = await bound.ainvoke(messages)
        content = getattr(resp, "content", "")
        # Some providers return content as a list of blocks; join text blocks.
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        return content or ""

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _call_with_retry(
        self, messages: list[HumanMessage], max_tokens: int
    ) -> str:
        return await self._call_llm(messages, max_tokens)

    async def _invoke(self, messages: list[HumanMessage], max_tokens: int) -> str:
        """Call with retry + semaphore; raises on terminal failure."""
        async with self._sem:
            return await self._call_with_retry(messages, max_tokens)

    async def analyze(
        self,
        image_bytes: bytes,
        task: str,
        prompt: str,
        *,
        prompt_version: int = 1,
        max_tokens: int = 1024,
        expect_json: bool = True,
        force: bool = False,
        mime: str = "image/png",
        cache=None,
    ) -> dict | str | None:
        """Understand one image.

        Args:
            image_bytes: raw image bytes (PNG/JPEG).
            task: cache-bucket label ("figure" | "table" | "formula" | "ocr").
            prompt: instruction text (usually from the prompt registry).
            prompt_version: cache-version key; bump when the prompt changes.
            max_tokens: response cap.
            expect_json: True -> parse & return dict (one JSON retry, else None);
                         False -> return raw text (for OCR).
            force: bypass the cache read (still writes on success).
            mime: media type used in the image data URI.

        Returns: dict (expect_json), str (OCR text), or None on any failure.
        """
        if not image_bytes:
            return None

        is_open, remaining = self._breaker.is_open(_BREAKER_KEY)
        if is_open:
            logger.debug(
                "vision breaker open (%.1fs left); skipping %s", remaining, task
            )
            return None

        image_hash = hashlib.sha256(image_bytes).hexdigest()
        active_cache = cache or self._cache

        if not force:
            cached = active_cache.get(image_hash, task, prompt_version)
            if cached is not None:
                return cached

        messages = self._build_messages(prompt, image_bytes, mime)
        try:
            text = await self._invoke(messages, max_tokens)
        except _RETRYABLE as e:
            logger.warning("vision call exhausted retries (%s): %s", task, e)
            self._breaker.record_failure(_BREAKER_KEY)
            return None
        except (BadRequestError, AuthenticationError) as e:
            # Non-retryable: bad request / auth. One bad image must not open the
            # breaker for the whole provider — log and degrade this element only.
            logger.warning("vision call rejected (%s): %s", task, e)
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("vision call failed (%s): %s", task, e)
            self._breaker.record_failure(_BREAKER_KEY)
            return None

        self._breaker.record_success(_BREAKER_KEY)

        if not expect_json:
            result: Any = (text or "").strip()
            if not result:
                return None
            active_cache.put(image_hash, task, prompt_version, result)
            return result

        parsed = _parse_json_lenient(text)
        if parsed is None:
            # One soft retry with a stricter instruction (cheap insurance).
            stricter = (
                prompt
                + "\n\n（重要：只输出一个合法 JSON 对象，不要任何代码块或解释文字）"
            )
            try:
                text2 = await self._invoke(
                    self._build_messages(stricter, image_bytes, mime), max_tokens
                )
                parsed = _parse_json_lenient(text2)
            except Exception as e:  # noqa: BLE001
                logger.debug("vision JSON retry failed (%s): %s", task, e)
                parsed = None
        if parsed is None:
            logger.info("vision %s produced unparseable JSON; degrading", task)
            return None
        active_cache.put(image_hash, task, prompt_version, parsed)
        return parsed


_CLIENT: VisionClient | None = None


def get_vision_client() -> VisionClient | None:
    """Return the singleton VisionClient, or None when vision is not configured.

    None means "degrade to text-only"; callers must handle it. Config is
    re-read while unconfigured (cheap field access) and the client is cached
    once successfully built. A construction failure also yields None.
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    cfg = get_settings().multimodal
    if not (cfg.api_key and cfg.base_url and cfg.model):
        return None
    try:
        _CLIENT = VisionClient(cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("vision client init failed; degrading to text-only: %s", e)
        _CLIENT = None
    return _CLIENT
