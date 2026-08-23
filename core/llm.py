"""LLM client factory for DeepSeek via LangChain (DESIGN D-001, D-005, D-062).

D-062: RateLimitedLLM wraps ChatOpenAI with a global concurrency semaphore
to prevent 429 errors when multiple agents call the API simultaneously.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any, AsyncIterator

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from core.config import get_settings

# Utility calls (query rewrite, cluster labels, history compression, ...) have
# no use for a thinking phase. Ask the endpoint to skip it; endpoints that
# reject the parameter fall back to a plain call.
_UTILITY_EXTRA_BODY = {"thinking": {"type": "disabled"}}


def _patch_langchain_reasoning() -> None:
    """Bridge DeepSeek thinking mode through langchain-openai.

    langchain-openai deliberately does NOT extract `reasoning_content` from
    streamed deltas ("use a provider-specific subclass"), and this endpoint
    (deepseek-v4-flash, thinking mode) answers 400 unless the reasoning text
    is passed back on any assistant message that carries tool_calls. Patch
    the two module-level converters once per process: capture reasoning into
    chunk.additional_kwargs on the way in, echo it into the request dict on
    the way out. Idempotent; no-op for endpoints without a reasoning channel.
    """
    import langchain_openai.chat_models.base as lc_base
    from langchain_core.messages import AIMessage, AIMessageChunk

    if getattr(lc_base, "_reasoning_patch_applied", False):
        return

    orig_delta = lc_base._convert_delta_to_message_chunk

    def delta_with_reasoning(_dict, default_class):
        chunk = orig_delta(_dict, default_class)
        rc = _dict.get("reasoning_content")
        if rc and isinstance(chunk, AIMessageChunk):
            chunk.additional_kwargs["reasoning_content"] = rc
        return chunk

    orig_msg = lc_base._convert_message_to_dict

    def message_with_reasoning(message, api="chat/completions"):
        d = orig_msg(message, api=api)
        if isinstance(message, AIMessage):
            rc = (message.additional_kwargs or {}).get("reasoning_content")
            if rc and "tool_calls" in d:
                d["reasoning_content"] = rc
        return d

    lc_base._convert_delta_to_message_chunk = delta_with_reasoning
    lc_base._convert_message_to_dict = message_with_reasoning
    lc_base._reasoning_patch_applied = True


_patch_langchain_reasoning()


async def ainvoke_utility(llm: Any, messages: list[BaseMessage], **call_kwargs: Any) -> Any:
    """ainvoke with the thinking phase disabled; plain-call fallback on 400.

    langchain-openai forwards per-call kwargs into the request payload, and
    the openai client merges extra_body into the JSON body — so the per-call
    extra_body reaches the endpoint. If the endpoint does not understand the
    thinking parameter it answers 400, in which case we retry without it.
    """
    try:
        return await llm.ainvoke(messages, extra_body=_UTILITY_EXTRA_BODY, **call_kwargs)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "400" in msg or "thinking" in msg or "extra_body" in msg:
            return await llm.ainvoke(messages, **call_kwargs)
        raise


class RateLimitedLLM:
    """Thin wrapper around ChatOpenAI that limits concurrent API calls.

    LangChain's max_retries handles 429 retransmission, but multiple agents
    (search, chat_turn, review) can fire simultaneous requests. This wrapper
    caps global concurrency at max_concurrent so we never exceed the
    API rate limit in the first place.
    """

    _semaphores: dict[str, asyncio.Semaphore] = {}

    def __init__(self, llm: ChatOpenAI, max_concurrent: int = 3):
        self._llm = llm
        self.model = llm.model
        key = f"{llm.model}"
        if key not in RateLimitedLLM._semaphores:
            RateLimitedLLM._semaphores[key] = asyncio.Semaphore(max_concurrent)
        self._sem = RateLimitedLLM._semaphores[key]

    async def ainvoke(
        self, messages: list[BaseMessage], **kwargs: Any
    ) -> Any:
        async with self._sem:
            return await self._llm.ainvoke(messages, **kwargs)

    async def astream(
        self, messages: list[BaseMessage], **kwargs: Any
    ) -> AsyncIterator[Any]:
        async with self._sem:
            async for chunk in self._llm.astream(messages, **kwargs):
                yield chunk

    def bind_tools(self, tools, **kwargs: Any):
        """Bind tools preserving the per-model semaphore (D-062/D-084).

        ChatOpenAI.bind_tools returns a bare _ChatModelBinding whose astream
        bypasses this wrapper's semaphore, silently dropping D-062's 429
        protection on the native-function-calling path. Wrap the bound model
        so the concurrency limit still applies.
        """
        bound = self._llm.bind_tools(tools, **kwargs)
        return RateLimitedBoundLLM(bound, self._sem, self.model)

    # Pass-through for any other ChatOpenAI attributes
    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)


class RateLimitedBoundLLM:
    """A tool-bound LLM that preserves RateLimitedLLM's concurrency limit.

    Returned by RateLimitedLLM.bind_tools so the native-function-calling path
    (D-084 P0) keeps D-062's per-model semaphore. Delegates ainvoke/astream
    under the semaphore; everything else falls through to the bound model.
    """

    def __init__(self, bound, semaphore: asyncio.Semaphore, model: str):
        self._bound = bound
        self._sem = semaphore
        self.model = model
        # langchain exposes model_name on bound models; mirror for trace.
        self.model_name = getattr(bound, "model_name", model)

    async def ainvoke(self, messages: list[BaseMessage], **kwargs: Any) -> Any:
        async with self._sem:
            return await self._bound.ainvoke(messages, **kwargs)

    async def astream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[Any]:
        async with self._sem:
            async for chunk in self._bound.astream(messages, **kwargs):
                yield chunk

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bound, name)


@lru_cache(maxsize=4)
def get_llm(model_tier: str = "light") -> RateLimitedLLM:
    """Return a cached RateLimitedLLM wrapping ChatOpenAI for DeepSeek.

    Args:
        model_tier: "light" (DeepSeek-V3) or "reasoning" (DeepSeek-R1).
    """
    s = get_settings().llm
    model = s.model_light if model_tier == "light" else s.model_reasoning
    inner = ChatOpenAI(
        model=model,
        api_key=s.api_key,
        base_url=s.base_url,
        temperature=s.temperature,
        max_retries=s.max_retries,
        timeout=s.timeout,
        stream_usage=True,  # emit usage_metadata on the final stream chunk
    )
    return RateLimitedLLM(inner, max_concurrent=3)
