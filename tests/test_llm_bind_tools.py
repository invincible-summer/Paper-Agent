"""Tests for RateLimitedLLM.bind_tools (D-084 P0).

The native-function-calling path must keep D-062 per-model concurrency —
ChatOpenAI.bind_tools returns a bare binding that bypasses the wrapper.
This verifies the bound wrapper re-applies the semaphore and streams
tool_call_chunks through unchanged.
"""
import asyncio

from core.llm import RateLimitedLLM, RateLimitedBoundLLM


class _FakeChunk:
    def __init__(self, content=None, tool_call_chunks=None):
        self.content = content
        self.tool_call_chunks = tool_call_chunks or []


class _FakeBound:
    """Minimal stand-in for a langchain _ChatModelBinding."""
    def __init__(self):
        self.astream_called = False
        self.ainvoke_called = False
        self.model_name = "fake-model"

    async def astream(self, messages, **kwargs):
        self.astream_called = True
        yield _FakeChunk(content="hello", tool_call_chunks=[
            {"index": 0, "name": "search", "args": '{"topic": "x"}'}
        ])

    async def ainvoke(self, messages, **kwargs):
        self.ainvoke_called = True
        return _FakeChunk(content="hi")


def test_bound_llm_streams_tool_call_chunks():
    bound = _FakeBound()
    sem = asyncio.Semaphore(1)
    wrapped = RateLimitedBoundLLM(bound, sem, model="fake-model")

    async def _collect():
        out = []
        async for c in wrapped.astream([]):
            out.append(c)
        return out

    chunks = asyncio.run(_collect())
    assert bound.astream_called
    assert chunks and chunks[0].content == "hello"
    assert chunks[0].tool_call_chunks[0]["name"] == "search"


def test_bound_llm_ainvoke_delegates():
    bound = _FakeBound()
    sem = asyncio.Semaphore(1)
    wrapped = RateLimitedBoundLLM(bound, sem, model="fake-model")
    res = asyncio.run(wrapped.ainvoke([]))
    assert bound.ainvoke_called
    assert res.content == "hi"


def test_bound_llm_holds_semaphore_during_stream():
    """The wrapper must hold the per-model semaphore for the whole stream."""
    bound = _FakeBound()
    sem = asyncio.Semaphore(1)
    wrapped = RateLimitedBoundLLM(bound, sem, model="fake-model")
    held = {"v": False}

    async def _run():
        async def _stream():
            async for _ in wrapped.astream([]):
                if sem._value == 0:
                    held["v"] = True
        t = asyncio.create_task(_stream())
        await asyncio.sleep(0.05)
        await t

    asyncio.run(_run())
    assert held["v"], "semaphore must be held during astream (D-062 preserved)"


def test_rate_limited_llm_bind_tools_returns_bound_wrapper():
    """bind_tools must return RateLimitedBoundLLM, not a bare _ChatModelBinding."""
    class _DummyChat:
        model = "dummy"
        model_name = "dummy"
        def bind_tools(self, tools, **kwargs):
            return _FakeBound()
    rl = RateLimitedLLM.__new__(RateLimitedLLM)
    rl._llm = _DummyChat()
    rl.model = "dummy"
    rl._sem = asyncio.Semaphore(2)
    bound = rl.bind_tools([])
    assert isinstance(bound, RateLimitedBoundLLM),         "bind_tools must wrap in RateLimitedBoundLLM to preserve D-062 semaphore"
