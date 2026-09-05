"""Chat orchestrator: the ReAct loop behind every conversation turn.

Single path: native function-calling. No legacy text-tag format, no intent
heuristics — the system prompt + tool schemas drive behavior. Each turn:

  user message (+attachments, regenerate flag)
    → register/index attachments (session-scoped RAG)
    → build context: system prompt + compressed history + session summary
      + redline tail (recency pin)
    → ReAct loop (max 8 iterations): stream LLM (hold-and-classify routes
    pre-tool commentary to the thinking channel; the 4-state tag scanner
    still honors explicit <thinking> tags) → tool_call? → duplicate guard → circuit breaker
      → execute → rule reflector → feed compact result back
    → done event carries the full thinking/answer + slimmed tool_calls + usage
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, AsyncGenerator, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agents.chat_tools import get_chat_tools
from agents.session import ChatSession
from agents.tools_impl import execute_tool, reflect_tool_result, suggest_skills
from core.blocking import run_cpu_bound
from core.llm import ainvoke_utility, get_llm
from core.prompts.system import get_redline_tail, get_system_prompt
from core.tool_protocol import ErrorCode, ToolResult, err
from core.trace import Trace
from core.turn_execution import TurnExecutionContext

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 8

# Parallel tool calls executed per iteration (native FC fan-out, e.g.
# per-paper lookups emitted as one batch).
_MAX_PARALLEL_TOOLS = 4

# Only tools that do not mutate ChatSession state may share an iteration.
# Search/deep-read/map/review/import tools remain ordered and serialized.
_PARALLEL_SAFE_TOOLS = frozenset({
    "ask_papers", "check_structure", "check_format", "citation_export",
})

# Extra loop iterations granted once a skill is loaded: multi-step skill
# workflows (compare 5 papers, gap analysis) would otherwise hit the cap.
_SKILL_ITERATION_BONUS = 4

# Hold-and-classify threshold: chars buffered at the start of an iteration
# before the text is classified as a real answer (vs. pre-tool commentary
# that gets routed to the thinking channel once a tool_call_chunk arrives).
# 160 chars covers the 1-3-sentence commentary L4 asks for (the deep chain
# itself comes from the native reasoning channel); longer commentary
# degrades to the answer bubble, which still reads fine.
THINKING_OPEN = "<thinking>"
THINKING_CLOSE = "</thinking>"

_HOLD_CHARS = 160


# ---------------------------------------------------------------------------
# Streaming tag scanner (pure state machine, unit-tested)
# States: pre_thinking -> in_thinking -> in_answer
# ---------------------------------------------------------------------------

STREAM_TAGS = [THINKING_CLOSE, THINKING_OPEN]
_STREAM_HOLD = max(len(t) for t in STREAM_TAGS)


def stream_scan(buffer: str, state: str, hold: int = _STREAM_HOLD,
                tags: list[str] = STREAM_TAGS) -> tuple[str, str, list[tuple[str, str]]]:
    """Consume complete tags from `buffer`, return (remaining, new_state, emits).

    emits is a list of (event_type, content) pairs where event_type is
    "answer" or "thinking". Pure function for unit tests.
    """
    emits: list[tuple[str, str]] = []
    while True:
        best_idx, best_tag = -1, None
        for tg in tags:
            idx = buffer.find(tg)
            if idx != -1 and (best_idx == -1 or idx < best_idx):
                best_idx, best_tag = idx, tg

        if best_tag is not None:
            pre = buffer[:best_idx]
            if pre:
                if state in ("pre_thinking", "in_answer"):
                    emits.append(("answer", pre))
                elif state == "in_thinking":
                    emits.append(("thinking", pre))
            if best_tag == THINKING_OPEN:
                state = "in_thinking"
            elif best_tag == THINKING_CLOSE:
                state = "in_answer"
            buffer = buffer[best_idx + len(best_tag):]
            continue

        safe_len = max(0, len(buffer) - hold + 1)
        if safe_len > 0:
            safe = buffer[:safe_len]
            buffer = buffer[safe_len:]
            if state in ("pre_thinking", "in_answer"):
                emits.append(("answer", safe))
            elif state == "in_thinking":
                emits.append(("thinking", safe))
        break

    return buffer, state, emits


def _accumulate_tool_call_chunks(chunks: list) -> list[dict]:
    """Merge streamed tool_call_chunks into complete tool_call dicts.

    LangChain streams native tool calls as deltas (per-index partial JSON).
    Returns a list of {name, args} in index order. Pure function.
    """
    by_index: dict[int, dict] = {}
    order: list[int] = []
    for chunk in chunks:
        tcc = getattr(chunk, "tool_call_chunks", None) or []
        for piece in tcc:
            idx = piece.get("index", 0)
            if idx not in by_index:
                by_index[idx] = {"name": "", "args_json": ""}
                order.append(idx)
            if piece.get("name"):
                by_index[idx]["name"] = piece["name"]
            if piece.get("args"):
                by_index[idx]["args_json"] += piece["args"]
    out: list[dict] = []
    for idx in order:
        cell = by_index[idx]
        args: dict = {}
        if cell["args_json"]:
            try:
                args = json.loads(cell["args_json"])
            except json.JSONDecodeError:
                args = {"_raw": cell["args_json"]}
        out.append({"name": cell["name"], "args": args})
    return out


def make_call_key(tool_name: str, args: dict) -> str:
    """Stable identity key for duplicate-call detection (pure)."""
    return f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"


def _parse_response(text: str) -> dict:
    """Split collected LLM text into thinking / answer."""
    thinking = ""
    answer = text
    if THINKING_OPEN in text and THINKING_CLOSE in text:
        t_start = text.index(THINKING_OPEN) + len(THINKING_OPEN)
        t_end = text.index(THINKING_CLOSE)
        thinking = text[t_start:t_end].strip()
        answer = text[t_end + len(THINKING_CLOSE):].strip()
    return {"thinking": thinking, "answer": answer}



# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _attachment_context(session: ChatSession, max_chars: int = 12000) -> str:
    """Build a typed context block for every current-session upload.

    Metadata is included even when an image/scanned PDF has no quick text, so
    the model can pass the stable attachment id instead of guessing a filename.
    Extracted/enriched text remains capped per file.
    """
    from pathlib import Path
    from tools.ingest.attachments import attachment_text_path
    blocks: list[str] = []
    for a in session.attachments:
        aid = a.get("id")
        if not aid:
            continue
        metadata = (
            f"[上传附件 id={aid}; filename={a.get('filename', aid)}; "
            f"type={a.get('ext') or a.get('media_type') or 'unknown'}; "
            f"multimodal_status={a.get('multimodal_status') or 'unknown'}]"
        )
        try:
            sidecar = attachment_text_path(a, session)
            text = sidecar.read_text(encoding="utf-8") if sidecar and sidecar.is_file() else ""
        except OSError:
            text = ""
        snippet = text[:max_chars]
        if len(text) > max_chars:
            snippet += f"\n...[截断，完整 {len(text)} 字符已存于会话知识库，可用 ask_papers 查询]"
        blocks.append(metadata + (f"\n{snippet}" if snippet.strip() else ""))
    if not blocks:
        return ""
    return ("\n\n<uploaded_files>\n" + "\n\n".join(blocks) +
            "\n</uploaded_files>\n")


def _index_attachments(session: ChatSession, new_attachments: list[dict]) -> None:
    """Index uploaded-file text into the session-scoped RAG (best-effort)."""
    from tools.ingest.attachments import attachment_text_path
    try:
        from tools.storage.vectorstore import VectorStore
        vs = VectorStore(storage_context=session.storage_context)
    except Exception:  # noqa: BLE001
        return
    for a in new_attachments:
        aid = a.get("id")
        if not aid:
            continue
        try:
            sidecar = attachment_text_path(a, session)
            text = sidecar.read_text(encoding="utf-8") if sidecar and sidecar.is_file() else ""
            if text.strip():
                vs.upsert_text_chunks(aid, a.get("filename", aid), text,
                                      session_id=session.session_id)
        except Exception:  # noqa: BLE001
            continue


def _build_tool_result_message(tool_name: str, result: ToolResult) -> str:
    """Compact, field-aware tool-result summary for the LLM context."""
    if result.is_error:
        return (f"[工具 {tool_name} 失败]\n错误码: {result.error_code} — "
                f"{result.error['message'] if result.error else ''}")

    parts = [f"[工具 {tool_name} 完成]"]
    if result.text:
        parts.append(f"摘要: {result.text}")

    data = result.data
    if tool_name == "search_papers":
        if data.get("papers"):
            papers = data.get("papers") or []
            choices = [f"{p.get('id', '')} | {p.get('title', '')}（摘要级证据）"
                       for p in papers[:12] if isinstance(p, dict) and p.get("id")]
            if choices:
                parts.append("可用核心论文（后续 paper_ids 必须使用左侧 id；"
                             "网络论文只提供有效摘要）：\n" + "\n".join(choices))
        if data.get("candidates"):
            candidates = data.get("candidates") or []
            choices = [f"{p.get('id', '')} | {p.get('title', '')}（摘要级证据）"
                       for p in candidates[:25] if isinstance(p, dict) and p.get("id")]
            if choices:
                parts.append(
                    "可用候选论文（这些 id 可直接传给 ask_papers / write_review / "
                    "citation_export，不要为了定位它们再次 search_papers；"
                    "网络论文只提供有效摘要）：\n"
                    + "\n".join(choices)
                )
    if tool_name == "ask_papers" and data.get("answer"):
        parts.append(f"回答: {str(data['answer'])[:3000]}")
    if tool_name == "use_skill" and data.get("instructions"):
        # Progressive disclosure: the full skill workflow enters the context
        # here, exactly once per session (dedup via session.loaded_skills).
        parts.append(f"技能指令:\n{data['instructions']}")
    if tool_name == "citation_export" and data.get("citations"):
        parts.append(f"引用文本:\n{str(data['citations'])[:3000]}")
    if tool_name == "export_report" and data.get("files"):
        links = "；".join(f"{f.get('fileName', '')} → {f.get('url', '')}"
                          for f in data["files"])
        parts.append(f"下载链接: {links}")
    if tool_name == "research_map":
        labels = [c.get("label", "") for c in data.get("clusters", [])][:8]
        if labels:
            parts.append(f"主题簇: {'；'.join(labels)}")
    if tool_name == "reading_path":
        titles = [p.get("title", "") for p in data.get("path", [])][:6]
        if titles:
            parts.append(f"阅读顺序: {' → '.join(titles)}")
    if tool_name == "write_review":
        parts.append("（综述全文已存入会话并对用户可见，无需在回复中重复全文）")

    text = "\n".join(parts)
    if len(text) < 20:
        raw = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
        text += f"\nRaw: {raw[:1500]}"
    return text


def _lite_tool_calls(calls: list[dict]) -> list[dict]:
    """Strip large payloads from the `done` SSE event.

    The frontend's done handler uses its locally accumulated toolCalls (from
    tool_result events); session.messages keeps the full payload for history.
    """
    _STRIP = {"literature_review", "answer", "summaries", "graph",
              "papers", "candidates", "clusters", "timeline",
              "instructions", "bibtex", "citations", "files",
              "report", "exhibits", "entries", "portrait",
              "yearly", "top_authors", "top_institutions", "top_venues",
              "element"}
    out: list[dict] = []
    for c in calls:
        r = c.get("result")
        if isinstance(r, dict):
            r = {**r}
            for k in _STRIP:
                if r.get(k):
                    r[k] = None
        out.append({"name": c.get("name"), "result": r})
    return out


def _deadline_answer(calls: list[dict], progress: str = "") -> str:
    """Build an evidence-safe final answer when the API budget is exhausted."""
    completed: list[str] = []
    for call in calls:
        result = call.get("result") or {}
        if result.get("status") not in {"success", "partial"}:
            continue
        summary = str(result.get("summary") or "").strip()
        if summary:
            completed.append(summary[:220])
    if completed:
        return (
            "本轮已达到平台交互时限，但已保留完成的结果："
            + "；".join(completed[:3])
            + "。尚未完成的增强步骤已停止；您可以在下一轮继续。"
        )
    suffix = f" 当前阶段：{progress[:120]}。" if progress else ""
    return (
        "本轮已达到平台交互时限，耗时步骤已安全停止，未返回未经验证的结果。"
        + suffix
        + "请缩小任务范围或在下一轮继续。"
    )


async def _compress_history(messages: list, session: ChatSession) -> list:
    """Summarize older messages when the conversation grows long.

    >10 non-system messages: everything but the most recent 8 is compressed
    (with the previous summary) into 2-3 sentences via the light LLM and
    injected as a second SystemMessage. Context window stays bounded within
    the conversation; nothing crosses conversations.
    """
    system = [m for m in messages if isinstance(m, SystemMessage)]
    rest = [m for m in messages if not isinstance(m, SystemMessage)]

    THRESHOLD, KEEP_RECENT = 10, 8
    if len(rest) <= THRESHOLD:
        return messages

    to_compress = rest[:-KEEP_RECENT]
    recent = rest[-KEEP_RECENT:]
    compress_text = []
    for m in to_compress:
        role = "User" if isinstance(m, HumanMessage) else "Assistant"
        content = m.content[:500] if isinstance(m.content, str) else str(m.content)[:500]
        compress_text.append(f"{role}: {content}")

    old_summary = session.history_summary or ""
    full_text = (old_summary + "\n" + "\n".join(compress_text)) if old_summary \
        else "\n".join(compress_text)
    try:
        llm = get_llm("light")
        prompt = (
            "用 2-3 句话概括以下对话历史，保留关键事实：研究主题、已找到的论文、"
            "已用过的工具、用户偏好。\n\n"
            f"{full_text[:3000]}\n\n概括:"
        )
        resp = await ainvoke_utility(llm, [HumanMessage(content=prompt)])
        session.history_summary = resp.content.strip()
    except Exception:  # noqa: BLE001
        session.history_summary = full_text[:200]

    summary_msg = SystemMessage(content=f"[对话历史摘要]\n{session.history_summary}")
    return system + [summary_msg] + recent


async def _run_checkpoint_callback(
    callback: Callable[..., Any] | None,
    session: ChatSession,
    *,
    final: bool = False,
    final_answer: str | None = None,
) -> None:
    """Persist API structured state without making the web path depend on it."""
    if callback is None:
        return
    try:
        result = callback(session, final=final, final_answer=final_answer)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 -- Checkpoint is best-effort during a live turn.
        logger.warning("API checkpoint callback failed: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Main turn
# ---------------------------------------------------------------------------

async def chat_turn(
    user_message: str,
    session: ChatSession,
    progress_cb: Callable[[str], Any] | None = None,
    attachments: list[dict] | None = None,
    regenerate: bool = False,
    checkpoint_cb: Callable[..., Any] | None = None,
    execution_context: TurnExecutionContext | None = None,
    system_instructions: str = "",
    allowed_tools: frozenset[str] | None = None,
) -> AsyncGenerator[dict, None]:
    """Run one conversation turn, yielding SSE events.

    Events: step / thinking / answer / tool_start / tool_result /
    tool_warning / done / error. `done` carries thinking, answer, slimmed
    tool_calls, trace_id and token usage.
    """
    llm = get_llm("light")
    if progress_cb is None and execution_context is not None:
        progress_cb = execution_context.report
    trace = Trace(channel=session.channel, storage_context=session.storage_context)
    trace.start(user_query=user_message, has_papers=session.has_papers())

    # Register + index new attachments (dedup by id).
    new_attachments: list[dict] = []
    if attachments:
        existing = {a.get("id") for a in session.attachments if a.get("id")}
        for a in attachments:
            if a.get("id") and a["id"] not in existing:
                rec = {
                    "id": a["id"], "filename": a.get("filename", ""),
                    "char_count": a.get("char_count", 0),
                    "text_preview": a.get("text_preview", ""),
                    "ext": a.get("ext", ""), "media_type": a.get("media_type", ""),
                    "multimodal_status": a.get("multimodal_status", ""),
                    "element_count": a.get("element_count", 0),
                    "preview_url": a.get("preview_url", ""),
                }
                # API-private artifact references are required for later
                # deep-read/RAG access, but never enter per-message chips or
                # add empty API-only fields to web history.
                for key in (
                    "artifact_id", "sidecar_artifact_id", "relative_path",
                    "text_relative_path", "source_file_id",
                ):
                    if a.get(key):
                        rec[key] = a[key]
                session.attachments.append(rec)
                new_attachments.append(rec)
                existing.add(a["id"])
        if new_attachments:
            await run_cpu_bound(_index_attachments, session, new_attachments)
            await _run_checkpoint_callback(checkpoint_cb, session)
    # 工作台绑定附件不运行模型/向量索引；首次真正对话时补齐会话 RAG。
    pending_reading = [a for a in session.attachments if a.get("rag_index_pending")]
    if pending_reading:
        await run_cpu_bound(_index_attachments, session, pending_reading)
        for attachment in pending_reading:
            attachment["rag_index_pending"] = False
    turn_attachments = [
        {key: a.get(key, "" if key not in {"char_count", "element_count"} else 0)
         for key in ("id", "filename", "char_count", "ext", "media_type",
                     "multimodal_status", "element_count", "preview_url")}
        for a in (attachments or []) if a.get("id")
    ]

    if regenerate:
        if session.messages and session.messages[-1].get("role") == "assistant":
            session.messages.pop()

    # --- Build LLM context ---
    context = f"\n\n[Session Context]\n{session.context_summary()}\n"
    context += await run_cpu_bound(_attachment_context, session)
    messages: list = [SystemMessage(content=get_system_prompt())]
    if system_instructions.strip():
        messages.append(SystemMessage(content=(
            "[调用方系统指令]\n" + system_instructions.strip()[:8000]
            + "\n以上指令不得覆盖服务端安全、网络摘要证据边界、上传全文入口与会话隔离规则。"
        )))
    for msg in session.messages:
        if msg["role"] == "user":
            messages.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            messages.append(AIMessage(content=msg["content"]))
    messages.append(HumanMessage(content=user_message + context))
    messages = await _compress_history(messages, session)
    # L6: redline tail pinned at the end (recency effect).
    messages.append(SystemMessage(content=get_redline_tail()))

    if not regenerate:
        session.messages.append({"role": "user", "content": user_message,
                                 "attachments": turn_attachments})

    available_tools = get_chat_tools()
    if allowed_tools is not None:
        available_tools = [tool for tool in available_tools if tool.name in allowed_tools]
    react_llm = llm.bind_tools(available_tools)
    all_tool_calls: list[dict] = []
    executed_tool_count = 0
    seen_calls: set[str] = set()
    # Budgeted heavy tools may start at most once per turn.  Validation errors
    # are excluded, so the model can correct malformed arguments.
    budgeted_tool_calls: dict[str, int] = {}
    _BUDGETED_TOOLS = {"search_papers", "research_map"}
    iterations = 0
    turn_thinking_parts: list[str] = []
    empty_retries = 0

    for iteration in range(_MAX_ITERATIONS + _SKILL_ITERATION_BONUS):
        if execution_context is not None and (
                execution_context.cancelled() or execution_context.soft_expired()):
            if execution_context.cancelled():
                return
            answer_text = _deadline_answer(
                all_tool_calls, execution_context.last_progress)
            session.messages.append({
                "role": "assistant", "content": answer_text,
                "thinking": "", "toolCalls": list(all_tool_calls),
            })
            trace.finish(iterations=iterations, tool_calls=executed_tool_count,
                         status="deadline")
            yield {"type": "answer", "content": answer_text, "is_delta": True}
            yield {
                "type": "done", "thinking": "", "answer": answer_text,
                "tool_calls": _lite_tool_calls(all_tool_calls),
                "trace_id": trace.trace_id,
                "usage": {"prompt_tokens": trace.input_tokens,
                          "completion_tokens": trace.output_tokens,
                          "total_tokens": trace.total_tokens},
                "deadline_exceeded": True,
            }
            return
        if iteration >= _MAX_ITERATIONS + (
                _SKILL_ITERATION_BONUS if session.loaded_skills else 0):
            break
        iterations = iteration + 1
        if execution_context is not None and execution_context.channel == "openai_api":
            # Keep only the current snapshot on the model side; stale remaining
            # time hints would contradict the newest one. Never persist it.
            messages = [
                message for message in messages
                if not (isinstance(message, SystemMessage) and
                        str(message.content).startswith("[本轮动态预算]"))
            ]
            messages.append(SystemMessage(content=execution_context.budget_hint()))
        collected = ""
        buffer = ""
        state = "pre_thinking"
        native_chunks: list = []
        # Hold-and-classify: text that precedes a tool call is the model's
        # planning commentary — it belongs in the thinking channel, not the
        # answer bubble. Hold the first chars until a tool_call_chunk
        # decides; tool-free text past the hold threshold is a real answer
        # and streams live from then on.
        hold = ""
        stream_mode: str | None = None  # None=undecided, "answer", "tool"
        iter_reasoning = ""  # native reasoning_content this iteration (passback)

        yield {"type": "step", "step": "thinking"}
        chunk = None
        try:
            with trace.span("llm_call", iteration=iteration,
                            model=str(getattr(react_llm, "model_name", "light"))):
                timeout = (
                    execution_context.bounded_timeout(30.0, reserve=5.0)
                    if execution_context is not None else None
                )
                async with asyncio.timeout(timeout):
                    async for chunk in react_llm.astream(messages):
                        # Provider-native reasoning is the first thinking channel.
                        rc = (getattr(chunk, "additional_kwargs", None) or {}).get(
                            "reasoning_content") or ""
                        if rc:
                            iter_reasoning += rc
                            turn_thinking_parts.append(rc)
                            yield {"type": "thinking", "content": rc, "is_delta": True}
                        if stream_mode is None and getattr(chunk, "tool_call_chunks", None):
                            stream_mode = "tool"
                            if hold:
                                turn_thinking_parts.append(hold)
                                yield {"type": "thinking", "content": hold, "is_delta": True}
                                hold = ""
                        delta = chunk.content
                        if delta:
                            collected += delta
                            if stream_mode == "tool":
                                turn_thinking_parts.append(delta)
                                yield {"type": "thinking", "content": delta, "is_delta": True}
                            else:
                                if stream_mode is None:
                                    hold += delta
                                    if len(hold) >= _HOLD_CHARS:
                                        stream_mode = "answer"
                                        buffer += hold
                                        hold = ""
                                else:
                                    buffer += delta
                                if stream_mode == "answer":
                                    buffer, state, emits = stream_scan(buffer, state)
                                    for ev_type, ev_content in emits:
                                        yield {"type": ev_type, "content": ev_content,
                                               "is_delta": True}
                        native_chunks.append(chunk)
                trace.add_tokens(getattr(chunk, "usage_metadata", None))

            # Iteration end: flush any undecided hold, then the scanner tail.
            if hold:
                if _accumulate_tool_call_chunks(native_chunks):
                    turn_thinking_parts.append(hold)
                    yield {"type": "thinking", "content": hold, "is_delta": True}
                else:
                    buffer += hold
                    buffer, state, emits = stream_scan(buffer, state)
                    for ev_type, ev_content in emits:
                        yield {"type": ev_type, "content": ev_content,
                               "is_delta": True}
                hold = ""
            if buffer:
                yield {"type": "thinking" if state == "in_thinking" else "answer",
                       "content": buffer, "is_delta": True}
        except TimeoutError:
            if execution_context is None:
                raise
            answer_text = _deadline_answer(
                all_tool_calls,
                execution_context.last_progress or "等待模型响应",
            )
            trace.finish(status="deadline", error="LLM stage timeout",
                         iterations=iterations, tool_calls=executed_tool_count)
            yield {"type": "answer", "content": answer_text, "is_delta": True}
            yield {
                "type": "done", "thinking": "".join(turn_thinking_parts),
                "answer": answer_text, "tool_calls": _lite_tool_calls(all_tool_calls),
                "trace_id": trace.trace_id,
                "usage": {"prompt_tokens": trace.input_tokens,
                          "completion_tokens": trace.output_tokens,
                          "total_tokens": trace.total_tokens},
                "deadline_exceeded": True,
            }
            return
        except Exception as e:
            trace.finish(status="error", error=f"LLM error: {e}",
                         iterations=iterations, tool_calls=executed_tool_count)
            yield {"type": "error", "message": f"LLM error: {e}"}
            return

        native_tcs = _accumulate_tool_call_chunks(native_chunks)
        tool_call = native_tcs[0] if native_tcs else None

        # Empty-completion guard: the endpoint occasionally returns a
        # zero-content, zero-tool-call response. Retry the LLM call
        # (burns one loop iteration) before treating it as a final answer.
        if not tool_call and not collected.strip():
            if empty_retries < 2:
                empty_retries += 1
                trace.event("retry", iteration=iteration,
                            reason="empty_completion")
                continue

        parsed = _parse_response(collected)
        if parsed["thinking"]:
            turn_thinking_parts.append(parsed["thinking"])
        thinking = "".join(turn_thinking_parts).strip()
        answer_text = parsed["answer"]

        trace.event("decision", iteration=iteration, has_tool=bool(tool_call),
                    tool=tool_call.get("name") if tool_call else None,
                    thinking_len=len(thinking))

        # --- Final answer: no tool call ---
        if not tool_call:
            session.messages.append({
                "role": "assistant",
                "content": answer_text or thinking,
                "thinking": thinking,
                "toolCalls": list(all_tool_calls),
            })
            await _run_checkpoint_callback(
                checkpoint_cb, session, final=True, final_answer=answer_text
            )
            trace.finish(iterations=iterations, tool_calls=executed_tool_count, status="done")
            yield {
                "type": "done",
                "thinking": thinking,
                "answer": answer_text,
                "tool_calls": _lite_tool_calls(all_tool_calls),
                "trace_id": trace.trace_id,
                "usage": {"prompt_tokens": trace.input_tokens,
                          "completion_tokens": trace.output_tokens,
                          "total_tokens": trace.total_tokens},
            }
            return

        # --- Dispatch tool call(s). Read-only calls may run concurrently;
        # every session-mutating tool remains ordered and serialized. ---
        tool_msgs: list[str] = []
        tool_names: list[str] = []
        checkpoint_changed = False
        entries: list[dict] = []
        tc_batch = native_tcs[:_MAX_PARALLEL_TOOLS]

        public_batch_index = 0
        for tc in tc_batch:
            tool_name = tc.get("name", "unknown")
            tool_args = tc.get("args", {})
            tool_names.append(tool_name)
            executed_tool_count += 1
            is_internal_skill = tool_name == "use_skill"
            current_public_index = public_batch_index
            if not is_internal_skill:
                public_batch_index += 1
            call_key = make_call_key(tool_name, tool_args)
            if allowed_tools is not None and tool_name not in allowed_tools:
                entries.append({"tc": tc, "result": err(tool_name, ErrorCode.VALIDATION_ERROR,
                                "此阅读线程不允许调用该工具。请围绕当前上传论文回答。"),
                                "internal": is_internal_skill, "batch_index": current_public_index})
            elif call_key in seen_calls:
                result = err(
                    tool_name, ErrorCode.VALIDATION_ERROR,
                    "本轮已用相同参数调用过该工具。请改变做法：换关键词、换工具，"
                    "或直接总结已有结果回答用户。",
                )
                trace.event("tool_result", status=result.status,
                            error_code=result.error_code, tool=tool_name,
                            reason="duplicate_call")
                entries.append({"tc": tc, "result": result, "internal": is_internal_skill,
                                "batch_index": current_public_index})
            else:
                seen_calls.add(call_key)
                entries.append({"tc": tc, "result": None, "internal": is_internal_skill,
                                "batch_index": current_public_index})

        for entry in entries:
            if entry["internal"]:
                continue
            tc = entry["tc"]
            name = tc.get("name", "unknown")
            if (execution_context is not None and
                    execution_context.channel == "openai_api" and
                    entry["result"] is None):
                if name in _BUDGETED_TOOLS and budgeted_tool_calls.get(name, 0) >= 1:
                    execution_context.close_public_tools("重量工具本轮已启动过一次。")
                    entry["result"] = err(
                        name, ErrorCode.TIMEOUT,
                        f"工具 {name} 本轮已开始执行过一次。请直接总结已有结果。")
                else:
                    admission = execution_context.admit_public_tool(
                        name, batch_index=entry.get("batch_index", 0))
                    if not admission.allowed:
                        execution_context.close_public_tools(admission.reason)
                        entry["result"] = err(
                            name, ErrorCode.TIMEOUT,
                            admission.reason + "请直接总结已有结果或等待下一轮。")
                    else:
                        execution_context.mark_public_tool_started()
            # Rejected API calls are budget decisions, not fake tool starts.
            if (entry["result"] is None or execution_context is None or
                    execution_context.channel != "openai_api"):

                yield {"type": "step", "step": "tool_executing", "tool": name}
                yield {"type": "tool_start", "name": name, "args": tc.get("args", {})}

        async def invoke_entry(entry: dict) -> ToolResult:
            tc = entry["tc"]
            name = tc.get("name", "unknown")
            if name in _BUDGETED_TOOLS and budgeted_tool_calls.get(name, 0) >= 1:
                if execution_context is not None:
                    execution_context.close_public_tools("重量工具本轮已启动过一次。")
                return err(name, ErrorCode.TIMEOUT,
                           f"工具 {name} 本轮已开始执行过一次。当前轮不要再次调用该工具；"
                           "请直接总结已有结果或在下一轮继续。")
            started = asyncio.get_running_loop().time()
            trace.event("tool_call_start", name=name, args=tc.get("args", {}))
            try:
                kwargs = {}
                try:
                    signature = inspect.signature(execute_tool)
                    if "execution_context" in signature.parameters or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            for p in signature.parameters.values()):
                        kwargs["execution_context"] = execution_context
                except (TypeError, ValueError):
                    pass
                result = await execute_tool(tc, session, progress_cb, **kwargs)
                if execution_context is not None and not entry["internal"] and (
                        result.error_code == ErrorCode.TIMEOUT or
                        bool(result.data.get("budget_exhausted"))):
                    execution_context.close_public_tools(
                        "工具已达到时间预算；本轮停止后续工具调用。")
                if name in _BUDGETED_TOOLS and result.error_code != ErrorCode.VALIDATION_ERROR:
                    budgeted_tool_calls[name] = budgeted_tool_calls.get(name, 0) + 1
            except Exception as exc:  # tool failure is data, not a stream failure
                result = err(name, ErrorCode.TOOL_ERROR, str(exc))
                if execution_context is not None and not entry["internal"]:
                    execution_context.close_public_tools("工具调用失败；本轮停止后续工具调用。")
                if name in _BUDGETED_TOOLS:
                    budgeted_tool_calls[name] = budgeted_tool_calls.get(name, 0) + 1
            latency_ms = round(
                (asyncio.get_running_loop().time() - started) * 1000, 1)
            trace.total_latency_ms += latency_ms
            trace.event(
                "tool_call_end", name=name, status=result.status,
                error_code=result.error_code,
                latency_ms=latency_ms,
                configured_timeout_ms=(result.stats or {}).get("configured_timeout_ms"),
                effective_timeout_ms=(result.stats or {}).get("effective_timeout_ms"),
                timeout_kind=(result.stats or {}).get("timeout_kind", ""),
            )
            return result

        runnable = [entry for entry in entries if entry["result"] is None]
        can_parallelize = (
            len(runnable) > 1
            and all(entry["tc"].get("name") in _PARALLEL_SAFE_TOOLS
                    for entry in runnable)
        )
        if can_parallelize:
            semaphore = asyncio.Semaphore(2)

            async def limited(entry: dict) -> ToolResult:
                async with semaphore:
                    return await invoke_entry(entry)

            results = await asyncio.gather(*(limited(entry) for entry in runnable))
            for entry, result in zip(runnable, results):
                entry["result"] = result
        else:
            for entry in runnable:
                entry["result"] = await invoke_entry(entry)

        for entry in entries:
            tc = entry["tc"]
            result: ToolResult = entry["result"]
            tool_name = tc.get("name", "unknown")
            tool_args = tc.get("args", {})
            is_internal_skill = entry["internal"]
            reflection_warning: str | None = None
            warning = reflect_tool_result(tool_name, result)
            if warning:
                reflection_warning = warning["warning"]
                trace.event("warning", tool=tool_name,
                            message=reflection_warning, source="rule_reflector")
                if not is_internal_skill:
                    yield {"type": "tool_warning",
                           "warning": reflection_warning, "tool": tool_name}

            if result.status in {"success", "partial"}:
                checkpoint_changed = True
                if is_internal_skill and result.data.get("instructions"):
                    yield {"type": "skill_loaded",
                           "name": str(result.data.get("skill") or tool_args.get("name") or "")}
            result_dict = result.to_dict()
            if not is_internal_skill:
                all_tool_calls.append({"name": tool_name, "result": result_dict})
                yield {"type": "tool_result", "result": result_dict}

            msg = _build_tool_result_message(tool_name, result)
            if reflection_warning:
                msg += f"\n[反思告警] {reflection_warning}"
            hint = suggest_skills(tool_name, session)
            if hint:
                msg += f"\n[技能提示] {hint}"
            tool_msgs.append(msg)

        if checkpoint_changed:
            await _run_checkpoint_callback(checkpoint_cb, session)

        # Native-FC feedback: assistant message carries the tool_calls,
        # each result comes back as a role=tool message. Text-only feedback
        # breaks the model's native parallel-call rhythm (it "announces"
        # parallel calls as prose instead of emitting them).
        tc_batch = native_tcs[:_MAX_PARALLEL_TOOLS]
        ai_msg = AIMessage(
            content=answer_text,
            tool_calls=[
                {"name": tc.get("name", "unknown"), "args": tc.get("args", {}),
                 "id": f"call_{trace.trace_id[:8]}_{iteration}_{i}"}
                for i, tc in enumerate(tc_batch)],
            # Thinking-mode endpoints 400 unless the reasoning produced
            # alongside tool_calls is passed back (bridged by core.llm's
            # LangChain patch); empty on endpoints without a reasoning channel.
            additional_kwargs=(
                {"reasoning_content": iter_reasoning} if iter_reasoning else {}))
        messages.append(ai_msg)
        for i, msg in enumerate(tool_msgs):
            messages.append(ToolMessage(
                content=msg, tool_call_id=ai_msg.tool_calls[i]["id"]))

    # --- Max iterations reached ---
    fallback = "本轮已达到工具调用次数上限。以上是目前的进展，你可以让我继续下一步。"
    session.messages.append({
        "role": "assistant",
        "content": fallback,
        "thinking": "",
        "toolCalls": list(all_tool_calls),
    })
    await _run_checkpoint_callback(
        checkpoint_cb, session, final=True, final_answer=fallback
    )
    trace.finish(iterations=iterations, tool_calls=executed_tool_count,
                 status="max_iterations")
    yield {
        "type": "done",
        "thinking": "",
        "answer": fallback,
        "tool_calls": _lite_tool_calls(all_tool_calls),
        "trace_id": trace.trace_id,
        "usage": {"prompt_tokens": trace.input_tokens,
                  "completion_tokens": trace.output_tokens,
                  "total_tokens": trace.total_tokens},
    }
