"""Tests for the QA-depth + thinking-stream upgrade.

- qa.answer registered prompt is depth-adaptive (old brevity cap removed)
- system.main bumped with the open-ended-question top_k hint
- ask_papers tool-result message truncates at 3000 chars (was 1200)
- chat_turn streams provider-native reasoning and tagged thinking live
- ainvoke_utility passes extra_body and falls back when the endpoint 400s
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agents.orchestrator as orch
from agents.orchestrator import _build_tool_result_message, chat_turn
from agents.session import ChatSession
from core.llm import ainvoke_utility
from core.tool_protocol import ok


# --- qa.answer / system prompts ------------------------------------------------

def test_qa_answer_prompt_registered_and_depth_adaptive():
    import core.prompts  # noqa: F401 — triggers registration
    from core.prompts.registry import get

    p = get("qa.answer")
    assert p.version >= 1
    # The old "keep it tight" brevity cap must be gone.
    assert "3-8 sentences" not in p.text
    assert "Depth calibration" in p.text
    assert "NEVER compress" in p.text


def test_system_prompt_bumped_with_topk_hint():
    import core.prompts  # noqa: F401
    from core.prompts.registry import get

    p = get("system.main")
    assert p.version >= 3
    assert "top_k" in p.text and "8-10" in p.text


def test_search_result_context_exposes_stable_paper_ids():
    paper = {"id": "arxiv:1706.03762", "title": "Attention Is All You Need"}
    msg = orch._build_tool_result_message(
        "search_papers", ok("search_papers", "检索完成", papers=[paper]))
    assert "arxiv:1706.03762 | Attention Is All You Need" in msg
    assert "paper_ids" in msg


# --- 3000 truncation -------------------------------------------------------------

def test_ask_papers_answer_truncated_at_3000():
    r = ok("ask_papers", "完成回答", answer="x" * 4000, sources=[])
    msg = _build_tool_result_message("ask_papers", r)
    line = next(l for l in msg.splitlines() if l.startswith("回答: "))
    assert len(line) == len("回答: ") + 3000


def test_ask_papers_short_answer_not_padded():
    r = ok("ask_papers", "完成回答", answer="短回答", sources=[])
    msg = _build_tool_result_message("ask_papers", r)
    assert "短回答" in msg


# --- native reasoning_content merge ----------------------------------------------

class _FakeReasoningLLM:
    """Streams a native reasoning channel chunk + tagged thinking/answer."""

    model = "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        chunks = [
            SimpleNamespace(content="",
                            additional_kwargs={"reasoning_content": "原生推理 "},
                            tool_call_chunks=[], usage_metadata=None),
            SimpleNamespace(content="<thinking>标签思考</thinking>正式回答",
                            additional_kwargs={}, tool_call_chunks=[],
                            usage_metadata=None),
        ]
        for c in chunks:
            yield c


@pytest.mark.anyio
async def test_chat_turn_merges_native_reasoning_into_thinking(monkeypatch):
    monkeypatch.setattr(orch, "get_llm", lambda tier="light": _FakeReasoningLLM())
    session = ChatSession(session_id="t-reasoning")
    events = [ev async for ev in chat_turn("你好", session)]

    done = next(ev for ev in events if ev["type"] == "done")
    assert "原生推理" in done["thinking"]
    assert "标签思考" in done["thinking"]
    assert done["answer"] == "正式回答"

    thinking_deltas = "".join(
        ev["content"] for ev in events
        if ev["type"] == "thinking" and ev.get("is_delta"))
    assert "原生推理" in thinking_deltas
    assert "标签思考" in thinking_deltas
    assert "原生推理" in session.messages[-1].get("thinking", "")
    assert "标签思考" in session.messages[-1].get("thinking", "")


# --- pre-tool commentary → thinking channel ------------------------------------

class _FakeToolLLM:
    """Iteration 1: commentary + tool call. Iteration 2: the final answer."""

    model = "fake"

    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield SimpleNamespace(content="我先调用检索工具核实。",
                                  additional_kwargs={}, tool_call_chunks=[],
                                  usage_metadata=None)
            yield SimpleNamespace(content="", additional_kwargs={},
                                  tool_call_chunks=[{"index": 0, "name": "search_papers",
                                                     "args": '{"topic": "gnn"}'}],
                                  usage_metadata=None)
        else:
            yield SimpleNamespace(content="最终回答", additional_kwargs={},
                                  tool_call_chunks=[], usage_metadata=None)


@pytest.mark.anyio
async def test_pretool_commentary_streams_as_thinking(monkeypatch):
    from core.tool_protocol import ok as _ok

    fake = _FakeToolLLM()
    monkeypatch.setattr(orch, "get_llm", lambda tier="light": fake)

    async def fake_execute(tool_call, session, progress_cb):
        return _ok("search_papers", "检索完成：核心集 1 篇",
                   total_papers=1, core_titles=["A"])
    monkeypatch.setattr(orch, "execute_tool", fake_execute)

    session = ChatSession(session_id="t-pretool")
    events = [ev async for ev in chat_turn("查一下 gnn", session)]

    thinking_text = "".join(
        ev["content"] for ev in events
        if ev["type"] == "thinking" and ev.get("is_delta"))
    answer_text = "".join(
        ev["content"] for ev in events
        if ev["type"] == "answer" and ev.get("is_delta"))

    assert "我先调用检索工具核实" in thinking_text
    assert "我先调用检索工具核实" not in answer_text
    assert "最终回答" in answer_text

    done = next(ev for ev in events if ev["type"] == "done")
    assert "我先调用检索工具核实" in done["thinking"]
    assert done["answer"] == "最终回答"


class _FakeSkillLLM:
    model = "fake"

    def __init__(self):
        self.calls = 0
        self.second_messages = None

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield SimpleNamespace(
                content='我要调用 use_skill，参数 {"name":"structure_advisor"}',
                additional_kwargs={"reasoning_content": "下一步加载 structure_advisor skill"},
                tool_call_chunks=[], usage_metadata=None)
            yield SimpleNamespace(
                content="", additional_kwargs={},
                tool_call_chunks=[{"index": 0, "name": "use_skill",
                                   "args": '{"name":"structure_advisor"}'}],
                usage_metadata=None)
        else:
            self.second_messages = messages
            yield SimpleNamespace(content="已按工作流完成", additional_kwargs={},
                                  tool_call_chunks=[], usage_metadata=None)


@pytest.mark.anyio
async def test_use_skill_is_internal_but_instructions_reach_model(monkeypatch):
    fake = _FakeSkillLLM()
    monkeypatch.setattr(orch, "get_llm", lambda tier="light": fake)

    async def fake_execute(tool_call, session, progress_cb):
        return ok("use_skill", "技能 structure_advisor 已加载",
                  skill="structure_advisor", instructions="# 私有技能指令\n执行步骤")

    monkeypatch.setattr(orch, "execute_tool", fake_execute)
    events = [ev async for ev in chat_turn("检查结构", ChatSession(session_id="skill-hidden"))]

    visible = "".join(str(ev) for ev in events)
    assert "structure_advisor" in visible  # raw thinking is restored
    assert "私有技能指令" not in visible
    assert not any(ev["type"] in {"tool_start", "tool_result"} for ev in events)
    done = next(ev for ev in events if ev["type"] == "done")
    assert done["tool_calls"] == []
    assert fake.second_messages is not None
    assert any("私有技能指令" in str(getattr(m, "content", ""))
               for m in fake.second_messages)



@pytest.mark.anyio
async def test_parallel_tool_calls_all_execute(monkeypatch):
    """One iteration emitting two tool calls must execute both."""

    class _FakeParallelLLM:
        model = "fake"

        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def astream(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield SimpleNamespace(content="并行查两篇。",
                                      additional_kwargs={}, tool_call_chunks=[],
                                      usage_metadata=None)
                yield SimpleNamespace(
                    content="", additional_kwargs={},
                    tool_call_chunks=[
                        {"index": 0, "name": "ask_papers", "args": '{"query": "q1"}'},
                        {"index": 1, "name": "ask_papers", "args": '{"query": "q2"}'},
                    ], usage_metadata=None)
            else:
                yield SimpleNamespace(content="两篇都查完了", additional_kwargs={},
                                      tool_call_chunks=[], usage_metadata=None)

    from core.tool_protocol import ok as _ok

    fake = _FakeParallelLLM()
    monkeypatch.setattr(orch, "get_llm", lambda tier="light": fake)
    executed: list[str] = []

    async def fake_execute(tool_call, session, progress_cb):
        executed.append(tool_call["args"]["query"])
        return _ok("ask_papers", f"回答 {tool_call['args']['query']}",
                   answer="a", sources=[])
    monkeypatch.setattr(orch, "execute_tool", fake_execute)

    session = ChatSession(session_id="t-parallel")
    events = [ev async for ev in chat_turn("对比", session)]
    starts = [ev["name"] for ev in events if ev["type"] == "tool_start"]
    assert starts == ["ask_papers", "ask_papers"]
    assert executed == ["q1", "q2"]
    done = next(ev for ev in events if ev["type"] == "done")
    assert done["answer"] == "两篇都查完了"
    assert len(done["tool_calls"]) == 2


@pytest.mark.anyio
async def test_empty_completion_is_retried(monkeypatch):
    """A zero-content, zero-tool-call response must not end the turn."""

    class _FlakyEmptyLLM:
        model = "fake"

        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def astream(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return  # empty completion: yields nothing
            yield SimpleNamespace(content="正常回答", additional_kwargs={},
                                  tool_call_chunks=[], usage_metadata=None)

    fake = _FlakyEmptyLLM()
    monkeypatch.setattr(orch, "get_llm", lambda tier="light": fake)
    session = ChatSession(session_id="t-empty-retry")
    events = [ev async for ev in chat_turn("你好", session)]
    done = next(ev for ev in events if ev["type"] == "done")
    assert fake.calls == 2
    assert done["answer"] == "正常回答"


# --- ainvoke_utility ---------------------------------------------------------------

class _RecordingLLM:
    def __init__(self, fail_on_extra_body: bool = False):
        self.calls: list[dict] = []
        self.fail = fail_on_extra_body

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(kwargs)
        if self.fail and "extra_body" in kwargs:
            raise Exception("Error code: 400 - unknown parameter 'thinking'")
        return SimpleNamespace(content="ok")


@pytest.mark.anyio
async def test_ainvoke_utility_disables_thinking():
    llm = _RecordingLLM()
    resp = await ainvoke_utility(llm, [])
    assert resp.content == "ok"
    assert llm.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.anyio
async def test_ainvoke_utility_falls_back_on_400():
    llm = _RecordingLLM(fail_on_extra_body=True)
    resp = await ainvoke_utility(llm, [])
    assert resp.content == "ok"
    assert len(llm.calls) == 2
    assert "extra_body" not in llm.calls[1]


@pytest.mark.anyio
async def test_ainvoke_utility_reraises_non_400():
    class _Boom:
        async def ainvoke(self, messages, **kwargs):
            raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        await ainvoke_utility(_Boom(), [])
