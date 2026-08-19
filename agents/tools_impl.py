"""Chat tool implementations + validation + dispatch + rule reflector.

Tools: search_papers / deep_read / ask_papers / research_map /
reading_path / write_review / check_structure. All return the unified
ToolResult protocol.
Error messages carry embedded recovery instructions so the model reads what
to do next directly from the tool result.
"""
from __future__ import annotations

import asyncio
import inspect
import time
import logging
import re
from typing import Any, Callable

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from agents.chat_tools import ARGS_SCHEMAS
from agents.session import ChatSession
from core.blocking import run_cpu_bound
from core.circuit_breaker import get_breaker
from core.llm import ainvoke_utility, get_llm
from core.models import Paper
from core.tool_budget_store import CODE_TOOL_BUDGETS
from core.tool_protocol import ErrorCode, ToolResult, err, ok, partial_result
from core.turn_execution import ToolInvocationContext, TurnExecutionContext

logger = logging.getLogger(__name__)


# Code-level defaults; runtime overrides live in the tool-budget policy store
# (admin page /admin/performance) and win over these values per tool.
_TOOL_BUDGETS: dict[str, float] = dict(CODE_TOOL_BUDGETS)

# Compatibility alias for extensions that imported the old private name.
_API_TOOL_TIMEOUTS = _TOOL_BUDGETS


def _select_session_papers(session: ChatSession, paper_ids) -> list[Paper] | None:
    """Resolve requested ids to unique papers inside the current session only.

    ``ChatSession.paper_by_id`` accepts narrowly normalized DOI aliases so an
    LLM changing prefix/case does not trigger a wasteful recovery search.
    ``None`` means at least one requested id was unknown or ambiguous.
    """
    selected: list[Paper] = []
    seen: set[str] = set()
    for requested in paper_ids:
        if not requested:
            continue
        paper = session.paper_by_id(str(requested))
        if paper is None:
            return None
        if paper.id not in seen:
            selected.append(paper)
            seen.add(paper.id)
    return selected


def _sync_fulltext_status_from_summaries(session: ChatSession) -> None:
    """Papers whose summaries really carry full_text are definitely available."""
    from core.reading_policy import summary_has_full_text

    for paper in session.all_papers():
        if summary_has_full_text(session.paper_summaries.get(paper.id)):
            paper.fulltext_status = "available"


def _persist_fulltext_status(paper: Paper, status: str, evidence: str,
                             session: ChatSession) -> None:
    """Best-effort persistence of a verified fulltext status (never raises)."""
    try:
        from core.config import get_settings
        from tools.storage.database import Database

        db = Database(storage_context=session.storage_context) \
            if session.storage_context is not None \
            else Database(get_settings().storage.sqlite_path)
        try:
            db.set_fulltext_status(
                paper.id, status, evidence=evidence,
                pdf_path=paper.pdf_path or "",
                candidate_url=paper.pdf_url or "",
            )
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("fulltext status persist skipped for %s: %s", paper.id, e)


# ---------------------------------------------------------------------------
# Validation + dispatch
# ---------------------------------------------------------------------------

def validate_args(tool_name: str, args: dict) -> tuple[dict | None, ToolResult | None]:
    """Validate tool args against the pydantic schema (single source of truth)."""
    schema = ARGS_SCHEMAS.get(tool_name)
    if schema is None:
        return None, err(tool_name, ErrorCode.NO_TOOL,
                         f"未知工具 {tool_name}。可用工具：{', '.join(sorted(ARGS_SCHEMAS))}。")
    try:
        return schema(**(args or {})).model_dump(), None
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", [])) or "args"
        return None, err(
            tool_name, ErrorCode.VALIDATION_ERROR,
            f"参数 {loc} 不合法：{first.get('msg', e)}。请修正参数后重试，不要原样重发。",
        )


async def execute_tool(
    tool_call: dict,
    session: ChatSession,
    progress_cb: Callable[[str], Any] | None = None,
    execution_context: TurnExecutionContext | None = None,
) -> ToolResult:
    """Validate args, check the circuit breaker, dispatch to the implementation."""
    name = tool_call.get("name", "")
    args, err_result = validate_args(name, tool_call.get("args", {}))
    if err_result is not None:
        return err_result

    breaker = get_breaker()
    blocked = breaker.guard(name)
    if blocked is not None:
        return blocked

    impl = _IMPLS.get(name)
    if impl is None:
        return err(name, ErrorCode.NO_TOOL, f"未知工具：{name}")

    invocation_context: ToolInvocationContext | None = None

    async def call_impl() -> ToolResult:
        kwargs = {}
        try:
            signature = inspect.signature(impl)
            if "invocation_context" in signature.parameters:
                kwargs["invocation_context"] = invocation_context
        except (TypeError, ValueError):
            pass
        return await impl(args, session, progress_cb, **kwargs)

    async def invoke_impl() -> ToolResult:
        operation = {
            "deep_read": "deep_read", "exhibit_index": "deep_read",
            "explain_element": "deep_read", "export_report": "export",
            "export_manuscript": "export",
        }.get(name)
        if operation:
            from core.storage_pressure import StoragePressureError, guard_for_session
            guard = guard_for_session(session)
            if guard is not None:
                try:
                    async with guard.protect(operation, session.session_id):
                        return await call_impl()
                except StoragePressureError as exc:
                    return err(
                        name, ErrorCode.STORAGE_PRESSURE,
                        f"API 存储空间已达到 {exc.threshold}% 保护阈值，当前暂停文件型重任务；普通文字问答仍可继续。",
                    )
        return await call_impl()

    try:
        timeout = None
        timeout_kind = "tool_budget"
        preferred = _TOOL_BUDGETS.get(name, 30.0)
        if execution_context is not None:
            preferred = execution_context.configured_tool_budget(name)
            timeout, timeout_kind = execution_context.timeout_for(
                preferred, reserve=execution_context.reserve_seconds)
            execution_context.set_phase(f"tool:{name}")
        if timeout is not None:
            invocation_context = ToolInvocationContext(
                name=name, configured_timeout_seconds=preferred,
                effective_timeout_seconds=timeout, timeout_kind=timeout_kind,
                deadline=time.monotonic() + max(0.001, timeout - 0.75),
            )
        async with asyncio.timeout(timeout):
            result = await invoke_impl()
    except TimeoutError:
        if execution_context is not None:
            execution_context.last_timeout_kind = timeout_kind
            execution_context.timeout_count += 1
        budget_label = "整轮剩余时间" if timeout_kind == "turn_budget" else "工具自身预算"
        message = (
            f"工具 {name} 已达到{budget_label}，已停止该工具。"
            "当前轮不要再次调用该工具；请直接总结已有结果或在下一轮继续。"
        )
        logger.warning("tool_timeout name=%s timeout_kind=%s configured_timeout_ms=%s effective_timeout_ms=%s",
                       name, timeout_kind, round(preferred * 1000),
                       round((timeout or 0) * 1000))
        if timeout_kind == "tool_budget":
            breaker.record_failure(name)
        if invocation_context is not None and invocation_context.partial_result is not None:
            timed_out = invocation_context.partial_result
            timed_out.data.setdefault("budget_exhausted", True)
            timed_out.data.setdefault("timeout_kind", timeout_kind)
            timed_out.text = (timed_out.text.rstrip("。") +
                              "。后续增强步骤因时间预算停止，已保留当前结果。")
        else:
            timed_out = err(name, ErrorCode.TIMEOUT, message)
        timed_out.stats = {
            "configured_timeout_ms": round(preferred * 1000),
            "effective_timeout_ms": round((timeout or 0) * 1000),
            "timeout_kind": timeout_kind,
        }
        return timed_out
    except Exception:
        breaker.record_failure(name)
        raise

    if execution_context is not None:
        result.stats = {**(result.stats or {}),
                        "configured_timeout_ms": round(preferred * 1000),
                        "effective_timeout_ms": round((timeout or 0) * 1000) if timeout is not None else None,
                        "timeout_kind": timeout_kind}
    if result.error_code == ErrorCode.TOOL_ERROR:
        breaker.record_failure(name)
    else:
        breaker.record_success(name)
    return result


# ---------------------------------------------------------------------------
# search_papers
# ---------------------------------------------------------------------------

async def _tool_search_papers(args: dict, session: ChatSession, progress_cb,
                              invocation_context: ToolInvocationContext | None = None) -> ToolResult:
    from agents.search_agent import search_agent

    topic = (args.get("topic") or "").strip()
    if not topic:
        return err("search_papers", ErrorCode.VALIDATION_ERROR,
                   "缺少必填参数 topic。请先向用户确认研究主题，再带 topic 调用。")

    # Guard against "locate a paper we already own" searches. The model should
    # call deep_read/ask_papers with the session paper_id directly; this keeps
    # one bad dispatch decision from spending a full multi-source search.
    known_paper = session.paper_by_id(topic) if session.has_papers() else None
    if known_paper is None and session.has_papers():
        known_paper = session.paper_by_title(topic)
    if known_paper is not None:
        return err(
            "search_papers", ErrorCode.VALIDATION_ERROR,
            f"「{topic[:80]}」已经在当前会话论文集中（paper_id={known_paper.id}）。"
            "请直接调用 deep_read(paper_ids=[该 id]) 或 ask_papers(paper_id=该 id)，"
            "不要为定位已有论文重新 search_papers。",
        )

    session.topic = topic
    session.conception = args.get("conception") or session.conception
    session.language = args.get("language") or session.language

    def apply_snapshot(snapshot: dict) -> None:
        papers = [p if isinstance(p, Paper) else Paper.from_dict(p)
                  for p in snapshot.get("papers", [])]
        candidates = [p if isinstance(p, Paper) else Paper.from_dict(p)
                      for p in snapshot.get("candidates", [])]
        if not papers and not candidates:
            return
        session.papers = papers
        session.candidates = candidates
        session.sub_directions = list(snapshot.get("sub_directions", []))
        session.search_queries = list(snapshot.get("search_queries", []))
        session.map_data = {}
        session.reading_path = []
        session.literature_review = ""
        if invocation_context is not None:
            from core.reading_policy import normalize_fulltext_status
            total = len(papers) + len(candidates)
            text = (f"检索已获得可恢复结果：核心集 {len(papers)} 篇，"
                    f"候选 {len(candidates)} 篇。")
            invocation_context.publish_partial(partial_result(
                "search_papers", text, total_papers=total,
                core_titles=[p.title for p in papers[:10]],
                papers=[p.to_dict() for p in papers],
                candidates=[p.to_dict() for p in candidates],
                sub_directions=session.sub_directions,
                search_queries=session.search_queries,
                completed_stages=list(snapshot.get("completed_stages", [])),
                skipped_stages=list(snapshot.get("skipped_stages", [])),
                budget_exhausted=True,
                fulltext_unknown=sum(
                    normalize_fulltext_status(p.fulltext_status) == "unknown"
                    for p in papers + candidates),
            ))

    state: dict = {
        "topic": topic,
        "user_conception": session.conception or "",
        "language": session.language or "both",
        "session_id": session.session_id,
        "storage_context": session.storage_context,
        "snapshot_callback": apply_snapshot,
    }
    search_kwargs = {"progress_callback": progress_cb}
    try:
        signature = inspect.signature(search_agent)
        if "invocation_context" in signature.parameters:
            search_kwargs["invocation_context"] = invocation_context
    except (TypeError, ValueError):
        pass
    result = await search_agent(state, **search_kwargs)

    session.papers = [p if isinstance(p, Paper) else Paper.from_dict(p)
                      for p in result.get("papers", [])]
    session.candidates = [p if isinstance(p, Paper) else Paper.from_dict(p)
                          for p in result.get("candidates", [])]
    session.sub_directions = result.get("sub_directions", [])
    session.search_queries = result.get("search_queries", [])
    promoted_ids = list(result.get("promoted_fulltext_core_ids", []))
    fulltext_core_available = int(result.get("fulltext_core_available", 0) or 0)
    fulltext_core_target = int(result.get("fulltext_core_target", 0) or 0)
    # New search supersedes earlier derived artifacts.
    session.map_data = {}
    session.reading_path = []
    session.literature_review = ""

    total = len(session.papers) + len(session.candidates)
    if total == 0:
        return err(
            "search_papers", ErrorCode.NO_PAPERS,
            f"主题「{topic}」没有检索到论文。建议：换更宽泛的英文关键词、缩短过长的主题句、"
            "或拆成更具体的子问题后重试。",
        )
    from core.reading_policy import normalize_fulltext_status
    all_papers = session.papers + session.candidates
    statuses = [normalize_fulltext_status(p.fulltext_status) for p in all_papers]
    n_available = statuses.count("available")
    n_unavailable = statuses.count("unavailable")
    n_unknown = statuses.count("unknown")
    source_notices = [str(item) for item in result.get("source_notices", []) if str(item).strip()]
    text = (
        f"检索完成：核心集 {len(session.papers)} 篇，候选 {len(session.candidates)} 篇。"
        f"全文状态：{n_available} 篇已探测到可访问的 OA PDF，"
        f"{n_unavailable} 篇已探测为不可获取，"
        f"{n_unknown} 篇尚未完成探测（显示为待验证，深读时会实际下载尝试；"
        f"fulltext_status 字段：available=可获取全文 / "
        f"unavailable=仅摘要 / unknown=尚未验证；"
        f"探测只读取 PDF 文件头，不下载全文）。"
        f"核心层全文保障：{fulltext_core_available}/{fulltext_core_target} 篇；"
        f"本次从候选提升 {len(promoted_ids)} 篇（候选不补位）。"
        f"核心集论文：{'；'.join(p.title for p in session.papers[:5])}"
    )
    if source_notices:
        text += "\n来源提示：" + "；".join(source_notices)
    build_result = partial_result if result.get("budget_exhausted") else ok
    return build_result(
        "search_papers",
        text,
        core_titles=[p.title for p in session.papers[:10]],
        total_papers=total,
        fulltext_available=n_available,
        fulltext_unavailable=n_unavailable,
        fulltext_unknown=n_unknown,
        promoted_fulltext_core_ids=promoted_ids,
        promoted_fulltext_core_count=len(promoted_ids),
        fulltext_core_available=fulltext_core_available,
        fulltext_core_target=fulltext_core_target,
        sub_directions=session.sub_directions,
        search_queries=session.search_queries,
        source_notices=source_notices,
        search_route=result.get("search_route", {}),
        budget_exhausted=bool(result.get("budget_exhausted")),
        completed_stages=list(result.get("completed_stages", [])),
        skipped_stages=list(result.get("skipped_stages", [])),
        papers=[p.to_dict() for p in session.papers],
        candidates=[{"id": p.id, "title": p.title, "year": p.year,
                     "citation_count": p.citation_count, "source": p.source,
                     "urls": p.urls, "pdf_url": p.pdf_url, "doi": p.doi,
                     "fulltext_status": normalize_fulltext_status(p.fulltext_status)}
                    for p in session.candidates],
    )


# ---------------------------------------------------------------------------
# deep_read
# ---------------------------------------------------------------------------

async def _tool_deep_read(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    from agents.reader_agent import reader_agent
    from core.paper_search_settings_store import (
        disclose_fetch_policy, get_paper_search_policy, remote_download_allowed,
    )
    from core.reading_policy import plan_reading_depth, summary_has_full_text
    from tools.ingest.attachments import attachment_by_id, ensure_attachment_understood

    fetch_policy = get_paper_search_policy()
    explicit_remote_allowed = remote_download_allowed("explicit", fetch_policy)
    disclose_restriction = disclose_fetch_policy(fetch_policy)
    focus = args.get("focus") or ""
    paper_ids = [pid for pid in (args.get("paper_ids") or []) if pid]
    attachment_ids = [aid for aid in (args.get("attachment_ids") or []) if aid]

    if not session.has_papers() and not session.attachments:
        return err("deep_read", ErrorCode.NO_PAPERS,
                   "当前会话没有论文或上传附件。请先检索论文，或上传 PDF/DOCX/图片。")

    if paper_ids:
        selected = _select_session_papers(session, paper_ids)
        if selected is None:
            return err("deep_read", ErrorCode.VALIDATION_ERROR,
                       "部分 paper_ids 不在当前论文集中。请从本会话论文 id 中选择。")
    else:
        selected = (list(session.papers) if session.has_papers() and not attachment_ids else [])

    if attachment_ids:
        selected_attachments = [attachment_by_id(session, aid) for aid in attachment_ids]
        if any(a is None for a in selected_attachments):
            return err("deep_read", ErrorCode.VALIDATION_ERROR,
                       "部分 attachment_ids 不属于当前会话，已拒绝读取。")
        selected_attachments = [a for a in selected_attachments if a is not None]
    elif not selected and session.attachments:
        # Attachment-only conversations can simply ask to deep-read "this file".
        selected_attachments = list(session.attachments)
    else:
        selected_attachments = []

    attachment_results: list[dict] = []
    for attachment in selected_attachments:
        if progress_cb:
            progress_cb(f"正在按需深读上传文件：{attachment.get('filename', attachment.get('id', ''))[:40]}...")
        understood = await ensure_attachment_understood(attachment, session, focus=focus)
        attachment_results.append({"filename": attachment.get("filename", ""), **understood})

    # Deep read is an explicit full-text operation: every selected network paper
    # is a full-attempt candidate (highest relevance first). Papers already
    # carrying real full text are skipped; OA-unavailable papers fall back to
    # abstract inside reader_agent and are reported below as abstract-level.
    full_group, _ = plan_reading_depth(session, selected, focus) if selected else ([], [])
    full_group = [p for p in full_group
                  if not summary_has_full_text(session.paper_summaries.get(p.id))]
    already_full_count = len([p for p in selected
                              if summary_has_full_text(session.paper_summaries.get(p.id))])

    failures: list[dict] = []
    fallback_details: list[dict] = []
    n_summaries = 0
    if full_group:
        state: dict = {
            "papers": full_group, "topic": session.topic, "language": session.language,
            "field_profile": session.field_profile, "read_mode": "full",
            "session_id": session.session_id,
            "storage_context": session.storage_context,
            "fetch_origin": "explicit",
        }
        result = await reader_agent(state, progress_callback=progress_cb, read_mode="full")
        session.paper_summaries.update(result.get("paper_summaries", {}))
        n_summaries = len(result.get("paper_summaries", {}))
        failures.extend(result.get("read_failures", []))
        fallback_details.extend(result.get("fulltext_fallbacks", []))

    # Never count an attempted download as a full read. Actual full-text papers
    # are those whose summaries now carry a real parsed body.
    actual_full_ids = [
        p.id for p in full_group
        if summary_has_full_text(session.paper_summaries.get(p.id))
    ]
    session.full_read_count += len(actual_full_ids)

    # Some reader implementations/tests may not populate fulltext_fallbacks;
    # derive the same facts from the summaries so reporting is always exact.
    fallback_ids = {f.get("paper_id") for f in fallback_details}
    failure_ids = {f.get("paper_id") for f in failures}
    for p in full_group:
        summary = session.paper_summaries.get(p.id)
        if summary is None or p.id in fallback_ids or p.id in failure_ids:
            continue
        if not summary_has_full_text(summary):
            fallback_details.append({
                "paper_id": p.id, "title": p.title or p.id,
                "reason": ("oa_fulltext_unavailable" if explicit_remote_allowed
                           else "remote_fetch_restricted"),
            })
            fallback_ids.add(p.id)

    # Keep the per-paper fulltext_status in sync with what just happened, so a
    # later research_map/search report cannot regress to a guessed availability.
    for p in selected:
        if p.id in actual_full_ids or summary_has_full_text(session.paper_summaries.get(p.id)):
            p.fulltext_status = "available"
            _persist_fulltext_status(p, "available", "deep_read_full_verified", session)
        elif p.id in fallback_ids or p.id in failure_ids:
            restricted = any(
                detail.get("paper_id") == p.id and detail.get("reason") == "remote_fetch_restricted"
                for detail in fallback_details
            )
            status = "unknown" if restricted else "unavailable"
            p.fulltext_status = status
            _persist_fulltext_status(
                p, status, "remote_fetch_restricted" if restricted else "deep_read_full_unavailable", session)

    ready_uploads = sum(r.get("status") == "ready" for r in attachment_results)
    degraded_uploads = sum(r.get("status") in {"degraded", "legacy_text_only"} for r in attachment_results)
    deferred_uploads = sum(r.get("status") == "deferred" for r in attachment_results)
    parts: list[str] = []
    restricted_fallback = any(
        detail.get("reason") == "remote_fetch_restricted" for detail in fallback_details)
    if restricted_fallback and disclose_restriction:
        parts.append("管理员当前限制远程论文全文拉取，本次使用本地缓存和摘要级证据。")
    if selected:
        if full_group:
            if actual_full_ids:
                already_note = (
                    f"；另有 {already_full_count} 篇此前已全文级深读、直接复用"
                    if already_full_count else ""
                )
                parts.append(
                    f"论文深读完成：{len(actual_full_ids)} 篇已取得 OA 全文并完成全文级深读，"
                    f"{len(fallback_details)} 篇未取得全文、按摘要级深读；"
                    f"共返回 {n_summaries} 篇结构化摘要{already_note}"
                )
            else:
                if failures:
                    parts.append(
                        f"论文深读完成：{len(fallback_details)} 篇未取得 OA 全文、"
                        f"按摘要级深读；{len(failures)} 篇失败；"
                        f"共返回 {n_summaries} 篇结构化摘要"
                    )
                else:
                    parts.append(
                        f"论文深读完成：本次尝试的 {len(full_group)} 篇均未取得 OA 全文，"
                        f"已按摘要级深读；共返回 {n_summaries} 篇结构化摘要"
                    )
            if fallback_details:
                reason_labels = {
                    "oa_fulltext_unavailable": "无 OA 全文",
                    "fulltext_parse_degraded": "全文解析降级",
                    "remote_fetch_restricted": (
                        "管理员限制远程全文拉取" if disclose_restriction else "仅使用现有证据"),
                }
                shown = "；".join(
                    f"{f.get('title') or f.get('paper_id')}（"
                    f"{reason_labels.get(f.get('reason', ''), '无 OA 全文')}）"
                    for f in fallback_details[:6]
                )
                if len(fallback_details) > 6:
                    shown += f" 等 {len(fallback_details)} 篇"
                parts.append(f"未取得 OA 全文、按摘要级处理：{shown}")
        elif already_full_count:
            parts.append(f"所选 {already_full_count} 篇此前已全文级深读，直接复用全文与图表缓存")
        else:
            parts.append("所选论文没有可深读的网络论文")
    if attachment_results:
        parts.append(
            f"上传附件按需理解完成：{ready_uploads} 个已解析"
            + (f"，{degraded_uploads} 个降级为文本" if degraded_uploads else "")
            + (f"，{deferred_uploads} 个格式已保存但解析延期" if deferred_uploads else "")
        )
    if failures:
        parts.append(f"{len(failures)} 篇论文失败")
    selected_titles = {p.id: p.title for p in selected}
    summaries_data = {}
    for pid, summary in session.paper_summaries.items():
        if pid not in selected_titles:
            continue
        payload = summary.to_dict() if hasattr(summary, "to_dict") else dict(summary)
        summaries_data[pid] = {**payload, "title": selected_titles.get(pid) or pid}
    return ok(
        "deep_read", "；".join(parts) + "。", summaries=summaries_data,
        attachments=attachment_results, failures=failures,
        full_text_paper_ids=actual_full_ids,
        abstract_fallback_papers=fallback_details,
    )


# ---------------------------------------------------------------------------
# ask_papers (hybrid RAG: vector track + BM25 track, RRF fusion)
# ---------------------------------------------------------------------------

async def _tool_ask_papers(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    from core.reading_policy import evidence_gap, summary_has_full_text

    query = (args.get("query") or "").strip()
    if not query:
        return err("ask_papers", ErrorCode.VALIDATION_ERROR,
                   "缺少必填参数 query。请把用户的问题原样传入 query。")
    paper_id = (args.get("paper_id") or "").strip() or None
    attachment_id = (args.get("attachment_id") or "").strip() or None
    top_k = int(args.get("top_k") or 5)
    if paper_id and attachment_id:
        return err("ask_papers", ErrorCode.VALIDATION_ERROR,
                   "paper_id 与 attachment_id 互斥，请只限定一种来源。")

    if not session.paper_summaries and not session.has_papers() and not session.attachments:
        return err("ask_papers", ErrorCode.NO_PAPERS,
                   "会话中还没有可问答的内容。请先 search_papers（可再 deep_read），或让用户上传 PDF。")

    fulltext_missing_title = ""
    if paper_id:
        selected_paper = session.paper_by_id(paper_id)
        if selected_paper is None:
            return err("ask_papers", ErrorCode.VALIDATION_ERROR,
                       "指定 paper_id 不在当前会话论文集中。")
        paper_id = selected_paper.id
        had_fulltext = summary_has_full_text(session.paper_summaries.get(paper_id))
        fetched = await _ensure_fulltext(session, paper_id, progress_cb)
        if fetched and not had_fulltext:
            session.full_read_count += 1
        if not summary_has_full_text(session.paper_summaries.get(paper_id)):
            fulltext_missing_title = selected_paper.title or paper_id

    # Upload multimodal work is lazy: explicit attachment queries always parse
    # that attachment; figure/table/formula queries parse current multimodal
    # attachments once, then subsequent turns reuse fingerprint/image caches.
    from tools.ingest.attachments import (
        MULTIMODAL_EXTENSIONS, attachment_by_id, attachment_document_id,
        ensure_attachment_understood,
    )
    modality = _detect_modality(query)
    attachment_targets: list[dict] = []
    if attachment_id:
        attachment = attachment_by_id(session, attachment_id)
        if attachment is None:
            return err("ask_papers", ErrorCode.VALIDATION_ERROR,
                       "指定 attachment_id 不属于当前会话。")
        attachment_targets = [attachment]
        paper_id = attachment_document_id(attachment_id)
    elif modality:
        attachment_targets = [
            a for a in (session.attachments or [])
            if (a.get("ext") or "").lower() in MULTIMODAL_EXTENSIONS
            and a.get("multimodal_status") != "ready"
        ]
    attachment_results: list[dict] = []
    for attachment in attachment_targets:
        if progress_cb:
            progress_cb(f"正在按需理解上传文件：{attachment.get('filename', '')[:40]}...")
        understood = await ensure_attachment_understood(attachment, session, focus=query)
        attachment_results.append({"filename": attachment.get("filename", ""), **understood})

    # Self-heal: search-time vector indexing is best-effort and may have been
    # skipped (embedder cold-miss, restored history, ...). Re-index before
    # retrieval so a session with papers never degrades to BM25-only.
    await run_cpu_bound(_ensure_session_index, session)

    # Pipeline: query rewrite (best-effort) -> hybrid retrieve (2N) -> rerank (N).
    # Retrieval uses the rewritten query; generation sees the user's original.
    # Modality is detected from the ORIGINAL query (user intent), not the
    # rewritten one — the rewrite may drop a "Figure 3" cue even though the user
    # explicitly asked about a figure.
    retrieval_query = await _rewrite_query(query)
    structure_query = _is_structure_query(query)
    protected_outline = _section_outline_passage(session, paper_id) if structure_query else None
    passages = await run_cpu_bound(
        _retrieve_passages, session, retrieval_query, paper_id, top_k, modality
    )
    passages = _protect_passage(passages, protected_outline, top_k)
    if not passages:
        deferred = [item for item in attachment_results if item.get("status") == "deferred"]
        if deferred:
            return partial_result(
                "ask_papers",
                "指定附件已安全保存，但当前版本尚未提供 DOC/XLS/XLSX 解析能力，"
                "因此没有可用于 RAG 的文本段落；请转换为 DOCX、PDF、TXT 或 Markdown 后重试。",
                answer="", sources=[], attachments=attachment_results,
            )
        return partial_result(
            "ask_papers",
            "在本会话的论文与文件中没有检索到相关段落。可以换个包含论文关键词的问法重试，"
            "或带 paper_id 针对具体某篇问答（会尝试补读该篇 OA 全文；"
            "取不到全文时回退摘要级并明确说明）。",
            answer="", sources=[], attachments=attachment_results,
        )

    answer = await _generate_grounded_answer(query, passages)
    if answer is None:
        return partial_result(
            "ask_papers",
            "已检索到相关段落，但生成回答失败。请直接根据以下来源回答，或稍后重试。",
            answer="", sources=_passages_to_sources(passages),
        )

    # Adaptive depth (core/reading_policy): the generated answer shows an
    # evidence gap → escalate ONCE: targeted full-text fetch for the most
    # relevant papers, re-retrieve, regenerate. Rule-based, budget-capped,
    # no user confirmation.
    escalated = False
    if evidence_gap(answer, passages, query):
        n = await _escalate_fulltext(session, _escalation_targets(passages), progress_cb)
        if n:
            passages2 = await run_cpu_bound(
                _retrieve_passages, session, retrieval_query, paper_id, top_k, modality
            )
            passages2 = _protect_passage(
                passages2, _section_outline_passage(session, paper_id) if structure_query else None,
                top_k,
            )
            if passages2:
                answer2 = await _generate_grounded_answer(query, passages2)
                if answer2:
                    answer, passages, escalated = answer2, passages2, True

    valid_ids = {p["paper_id"] for p in passages if p.get("paper_id")}
    text = f"基于 {len(valid_ids)} 个来源、{len(passages)} 个段落完成回答。"
    if escalated:
        text += "（检测到摘要级证据不足，已自动补读全文后重新检索回答）"
    elif fulltext_missing_title:
        text += f"（注意：{fulltext_missing_title} 未能取得 OA 全文，本次回答仅基于摘要级证据）"
    elif evidence_gap(answer, passages, query):
        text += "（检测到证据缺口，但相关论文未能取得 OA 全文；请以上述摘要级边界为准）"
    return ok(
        "ask_papers",
        text,
        answer=answer, sources=_passages_to_sources(passages),
        attachments=attachment_results,
    )


_STRUCTURE_QUERY_RE = re.compile(
    r"章节结构|章节目录|论文结构|文章结构|有哪些(?:部分|章节)|每(?:个|一)(?:部分|章节)|"
    r"逐节|目录|section\s+outline|table\s+of\s+contents|paper\s+organization|"
    r"what\s+(?:are|is).*sections",
    re.IGNORECASE,
)


def _is_structure_query(query: str) -> bool:
    return bool(_STRUCTURE_QUERY_RE.search(query or ""))


def _section_outline_passage(session: ChatSession, paper_id: str | None) -> dict | None:
    if not paper_id:
        return None
    summary = session.paper_summaries.get(paper_id)
    outline = getattr(summary, "section_outline", None) or []
    if not outline:
        return None
    paper = session.paper_by_id(paper_id)
    lines = ["[Complete ordered section outline; authoritative structure metadata]"]
    for row in outline:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        start = int(row.get("page_start") or 0)
        end = int(row.get("page_end") or start or 0)
        page = f"p.{start}" if start and end == start else (f"pp.{start}-{end}" if start else "page unknown")
        lines.append(f"- {title} ({page})")
    if len(lines) == 1:
        return None
    return {
        "paper_id": paper_id,
        "title": paper.title if paper is not None else paper_id,
        "section": "section_outline",
        "text": "\n".join(lines),
    }


def _protect_passage(passages: list[dict], protected: dict | None, top_k: int) -> list[dict]:
    if protected is None:
        return passages[:top_k]
    rest = [p for p in passages if not (
        p.get("paper_id") == protected.get("paper_id")
        and p.get("section") == protected.get("section")
    )]
    return [protected] + rest[:max(0, top_k - 1)]


def _retrieve_passages(session: ChatSession, retrieval_query: str,
                       paper_id: str | None, top_k: int,
                       modality: str | None = None) -> list[dict]:
    """Hybrid retrieve (2N) + cross-encoder rerank (N)."""
    passages = _hybrid_retrieve(session, retrieval_query, paper_id=paper_id,
                                top_k=top_k * 2, modality=modality)
    return _rerank_passages(retrieval_query, passages, top_k)


async def _generate_grounded_answer(query: str, passages: list[dict]) -> str | None:
    """LLM answer over passages + citation validation. None on failure."""
    from core.prompts.registry import get
    from agents.review_agent import _validate_citations

    valid_ids = {p["paper_id"] for p in passages if p.get("paper_id")}
    context = "\n\n".join(
        f"[{p.get('paper_id', '?')}] [{p.get('section', 'summary')}] "
        f"{p.get('title', '')}\n{p.get('text', '')}" for p in passages
    )
    try:
        llm = get_llm("light")
        qa_prompt = get("qa.answer").text
        resp = await ainvoke_utility(
            llm, [HumanMessage(content=qa_prompt.format(query=query, context=context))])
        answer = (resp.content.strip() if hasattr(resp, "content") else str(resp)).strip()
    except Exception as e:  # noqa: BLE001
        logger.error("ask_papers LLM call failed: %s", e)
        return None
    return _validate_citations(answer, valid_ids)


def _escalation_targets(passages: list[dict]) -> list[str]:
    """Paper ids to full-read on escalation, ranked by passage order, deduped."""
    out: list[str] = []
    for p in passages:
        pid = p.get("paper_id")
        if pid and pid not in out:
            out.append(pid)
    return out


async def _escalate_fulltext(session: ChatSession, paper_ids: list[str],
                             progress_cb) -> int:
    """Budget-capped targeted full-text fetch; returns newly-fetched count."""
    from core.reading_policy import (
        FULL_READS_PER_ASK, full_read_allowance, summary_has_full_text,
    )

    allow = full_read_allowance(session, len(paper_ids), FULL_READS_PER_ASK)
    done = 0
    for pid in paper_ids[:allow]:
        s = session.paper_summaries.get(pid)
        if summary_has_full_text(s):
            continue  # already full — nothing to fetch, no budget spent
        if await _ensure_fulltext(session, pid, progress_cb):
            done += 1
    session.full_read_count += done
    if done:
        logger.info("ask_papers escalated to full text for %d paper(s)", done)
    return done


# --- modality detection (rule-based, zero LLM) --------------------------------
# When a question explicitly targets a figure / table / formula, we narrow the
# element vector track to that kind so "explain Figure 3" doesn't surface a
# formula. Cues are conservative: bare "表"/"图" are avoided (表示/图书 false
# positives); we require figure/table labels, numbers, or unambiguous nouns.
_MODALITY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("figure", re.compile(
        r"figure|fig\.?|图\s*\d|这张图|那幅图|这幅图|该图|此图|上图|下图|图示|如图|"
        r"图片|图像|示意图|架构图|曲线图|流程图|柱状图|diagram",
        re.IGNORECASE)),
    ("table", re.compile(
        r"table|tab\.?|表格|如表|表\s*\d|对比表|结果表|实验表",
        re.IGNORECASE)),
    ("formula", re.compile(
        r"formula|equation|公式|等式|方程",
        re.IGNORECASE)),
]


def _detect_modality(query: str) -> str | None:
    """Return 'figure' | 'table' | 'formula' | None from lexical cues.

    None means "search every element kind" (the default). Figure is checked
    first so "图3 里的公式" still resolves to the figure (the addressable
    container), not the formula inside it.
    """
    q = query or ""
    if not q.strip():
        return None
    for kind, pat in _MODALITY_PATTERNS:
        if pat.search(q):
            return kind
    return None


async def _rewrite_query(query: str) -> str:
    """One light-LLM pass turning a colloquial question into a retrieval query.

    Any failure / empty / implausible output falls back to the original query,
    so this pass is zero-risk.
    """
    from core.prompts.qa_prompts import QUERY_REWRITE_PROMPT

    try:
        llm = get_llm("light")
        resp = await ainvoke_utility(
            llm, [HumanMessage(content=QUERY_REWRITE_PROMPT.text.format(query=query))])
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        text = text.splitlines()[0].strip() if text else ""
        if not text or len(text) < 3 or len(text) > 300:
            return query
        return text
    except Exception as e:  # noqa: BLE001
        logger.debug("query rewrite failed, using raw query: %s", e)
        return query


def _rerank_passages(query: str, passages: list[dict], top_k: int) -> list[dict]:
    """Cross-encoder rerank over RRF-fused passages; falls back to RRF order."""
    if len(passages) <= 1:
        return passages
    from tools.retrieval.rerank import rerank

    order = rerank(query, [p.get("text", "") for p in passages], top_k=top_k)
    return [passages[i] for i in order]


def _ensure_session_index(session: ChatSession) -> None:
    """Re-index paper summaries when Chroma has nothing for this session.

    Search-time indexing (search_agent) is best-effort; when it was skipped
    the vector track goes silent and Chinese queries fall through BM25
    (English abstracts, zero token overlap) to "no passages". Local embeddings
    make this rebuild free and fast (~12 papers in seconds). Best-effort: any
    failure is logged and retrieval proceeds with whatever exists.
    """
    papers = session.all_papers()
    if not papers:
        return
    try:
        from tools.storage.vectorstore import VectorStore

        vs = VectorStore(storage_context=session.storage_context)
        if not vs.available():
            return
        if vs.count_papers(session.session_id) > 0:
            return
        logger.info("re-indexing %d paper summaries for session %s",
                    len(papers), session.session_id)
        for p in papers:
            vs.upsert_paper_summary(p, session.paper_summaries.get(p.id),
                                    session_id=session.session_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("session index self-heal skipped: %s", e)


async def _ensure_fulltext(session: ChatSession, paper_id: str, progress_cb=None) -> bool:
    """On-demand full-text for a single paper (targeted deep-dive / escalation).

    Fetches the PDF and runs it through the SAME multimodal pipeline as
    deep_read (parse → scanned recovery → VLM element understanding → persist +
    index), then indexes full-text chunks into the session RAG space and stashes
    full_text on the in-memory summary. Best-effort. Returns True when full text
    is available afterwards (already present or newly fetched).
    """
    from core.reading_policy import summary_has_full_text

    summary = session.paper_summaries.get(paper_id)
    if (summary_has_full_text(summary)
            and getattr(summary, "section_outline", None)
            and getattr(summary, "document_info", None)):
        paper = session.paper_by_id(paper_id)
        if paper is not None:
            paper.fulltext_status = "available"
            _persist_fulltext_status(paper, "available", "summary_full_verified", session)
        return True
    paper = session.paper_by_id(paper_id)
    if paper is None:
        return False
    from core.paper_search_settings_store import (
        disclose_fetch_policy, get_paper_search_policy, remote_download_allowed,
    )
    fetch_policy = get_paper_search_policy()
    if not remote_download_allowed("automatic", fetch_policy):
        if progress_cb and disclose_fetch_policy(fetch_policy):
            progress_cb("管理员当前限制自动论文全文拉取；本次使用已有缓存与摘要。")
        return False
    try:
        from agents.reader_agent import (
            _document_info, _section_outline, parse_and_understand,
        )
        from core.config import get_settings
        from core.reading_policy import is_full_text
        from tools.pdf.fetcher import PDFFetcher
        from tools.storage.database import Database
        from tools.storage.vectorstore import VectorStore

        if progress_cb:
            progress_cb(f"按需补读全文：{paper.title[:40]}...")
        fetcher = PDFFetcher(
            get_settings().reader.pdf_dir, storage_context=session.storage_context
        )
        paths = await fetcher.fetch_many([paper])
        await fetcher.close()
        pdf_path = paths.get(paper.id)
        if not pdf_path:
            paper.fulltext_status = "unavailable"
            _persist_fulltext_status(paper, "unavailable", "no_oa_fulltext", session)
            return False
        paper.pdf_path = pdf_path

        # Stage 1 + 1.5 + 2 + 3 — same path as deep_read. Element understanding
        # is fingerprint-gated, so escalating after a deep_read costs zero VLM.
        db = Database(storage_context=session.storage_context) if session.storage_context is not None else Database(get_settings().storage.sqlite_path)
        assets_dir = str(session.storage_context.blob_dir / "element_assets") if session.storage_context is not None and session.storage_context.channel == "openai_api" else get_settings().reader.assets_dir
        try:
            doc = await parse_and_understand(
                paper, db, assets_dir=assets_dir,
                storage_context=session.storage_context,
                session_id=session.session_id,
            )
        finally:
            db.close()
        if doc is None or not is_full_text(doc.raw_text):
            # A parse that only recovered a fragment/abstract must not be
            # labelled full text; ask_papers will state the abstract boundary.
            paper.fulltext_status = "unavailable"
            _persist_fulltext_status(paper, "unavailable", "parse_degraded", session)
            return False
        await run_cpu_bound(
            VectorStore(storage_context=session.storage_context).upsert_fulltext_chunks,
            paper, session_id=session.session_id,
            parsed_sections=doc.sections, full_text=doc.raw_text,
        )
        if summary is None:
            summary = PaperSummary(paper_id=paper.id)
            session.paper_summaries[paper.id] = summary
        summary.full_text = doc.raw_text
        summary.section_outline = _section_outline(doc)
        summary.document_info = _document_info(
            paper, doc, read_level="full", text_chars=len(doc.raw_text),
        )
        if doc.elements:
            summary.elements = [e.short_ref() for e in doc.elements]
        # Persist the healed/created lightweight structure metadata so restored
        # histories do not fall back to a top-k-only view on the next turn.
        cache_db = Database(storage_context=session.storage_context) \
            if session.storage_context is not None \
            else Database(get_settings().storage.sqlite_path)
        try:
            import json as _json
            cache_db.save_cached_summary(
                paper.id, session.field_profile or "general", "full",
                _json.dumps(summary.to_dict()),
            )
        finally:
            cache_db.close()
        paper.fulltext_status = "available"
        _persist_fulltext_status(paper, "available", "ask_escalation_full_verified", session)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("on-demand fulltext failed for %s: %s", paper_id, e)
        return False


def _element_passage_id(element_id: str) -> str:
    """Stable passage id for an element across the BM25 + vector tracks.

    Using one id shape for both tracks is what lets RRF fuse a lexical caption
    hit with a semantic understanding hit on the same element.
    """
    return f"{element_id}::element"


def _session_corpus(session: ChatSession) -> list[dict]:
    """In-memory BM25 corpus from session data (summaries + full texts + uploads
    + figure/table/formula element captions)."""
    from pathlib import Path

    from core.reading_policy import is_full_text
    from tools.storage.vectorstore import build_fulltext_chunks

    corpus: list[dict] = []
    title_of = {p.id: p.title for p in session.all_papers()}
    for p in session.all_papers():
        text = f"{p.title or ''}\n{p.abstract or ''}".strip()
        if text:
            corpus.append({"id": p.id, "paper_id": p.id, "title": p.title or "",
                           "section": "abstract", "text": text})
    for pid, s in (session.paper_summaries or {}).items():
        title = title_of.get(pid, pid)
        body_parts = [getattr(s, "research_problem", "") or "",
                      getattr(s, "methodology", "") or ""]
        findings = getattr(s, "key_findings", None) or []
        body_parts.extend(str(f) for f in findings[:5])
        body = "\n".join(b for b in body_parts if b.strip())
        if body.strip():
            corpus.append({"id": f"{pid}::summary", "paper_id": pid, "title": title,
                           "section": "summary", "text": body})
        outline_passage = _section_outline_passage(session, pid)
        if outline_passage is not None:
            corpus.append({"id": f"{pid}::section_outline", **outline_passage})
        full_text = getattr(s, "full_text", None)
        if is_full_text(full_text):
            corpus.extend(_parent_records(pid, title, build_fulltext_chunks(None, full_text)))
        # Element caption passages — let BM25 match "Figure 3" / "the architecture
        # diagram" lexically. The full VLM understanding text lives in the global
        # elements collection (queried by the vector track); here we only need the
        # addressable caption. Elements without a caption carry no lexical signal
        # and are skipped (the vector track still catches them semantically).
        for el in getattr(s, "elements", []) or []:
            eid = el.get("element_id") or ""
            cap = (el.get("caption") or "").strip()
            if not eid or not cap:
                continue
            kind = el.get("kind") or ""
            corpus.append({"id": _element_passage_id(eid), "paper_id": pid,
                           "title": title, "section": kind,
                           "text": f"[{kind}] {cap}"})
    from tools.ingest.attachments import attachment_text_path
    for a in session.attachments or []:
        aid = a.get("id")
        if not aid:
            continue
        try:
            sidecar = attachment_text_path(a, session)
            text = sidecar.read_text(encoding="utf-8") if sidecar and sidecar.is_file() else ""
        except OSError:
            continue
        corpus.extend(_parent_records(aid, a.get("filename", aid),
                                      build_fulltext_chunks(None, text)))
    # Uploaded elements are not represented by PaperSummary objects. Read only
    # the current session's upload namespaces from SQLite, preserving isolation.
    try:
        from core.config import get_settings
        from tools.ingest.attachments import attachment_document_id
        from tools.storage.database import Database
        db = Database(storage_context=session.storage_context) if session.storage_context is not None else Database(get_settings().storage.sqlite_path)
        try:
            for a in session.attachments or []:
                aid = a.get("id")
                if not aid:
                    continue
                doc_id = attachment_document_id(aid)
                for row in db.get_elements(doc_id):
                    eid = row.get("element_id") or ""
                    if not eid:
                        continue
                    payload = [row.get("caption") or ""]
                    payload.extend(str(v) for v in (row.get("docling_extract") or {}).values() if v)
                    payload.extend(str(v) for v in (row.get("understanding") or {}).values() if v)
                    text = "\n".join(x for x in payload if x).strip()
                    if text:
                        corpus.append({"id": _element_passage_id(eid), "paper_id": doc_id,
                                       "title": a.get("filename", aid),
                                       "section": row.get("kind", ""), "text": text})
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("uploaded element corpus unavailable: %s", e)
    return corpus


def _parent_records(doc_id: str, title: str, chunk_records: list[dict]) -> list[dict]:
    """Collapse child chunks into one BM25 corpus entry per parent passage.

    The vector track indexes small child chunks; the BM25 track indexes the
    parent passages — both keyed by parent_id so RRF fuses matching parents.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for i, rec in enumerate(chunk_records):
        parent_id = rec.get("parent_id") or f"c{i}"
        if parent_id in seen:
            continue
        seen.add(parent_id)
        out.append({"id": f"{doc_id}::{parent_id}", "paper_id": doc_id,
                    "title": title, "section": rec.get("section", ""),
                    "text": rec.get("parent_text") or rec.get("text", "")})
    return out


def _hybrid_retrieve(session: ChatSession, query: str, paper_id: str | None,
                     top_k: int, modality: str | None = None) -> list[dict]:
    """Vector track (session-scoped Chroma) + elements track (global, kind-filtered)
    + BM25 track (in-memory) → RRF.

    ``modality`` ("figure" | "table" | "formula" | None) narrows the elements
    vector track to one kind; None searches every kind. BM25 still sees element
    captions of all kinds (lexical "Figure 3" matches don't need a kind filter).
    """
    from tools.retrieval.bm25 import BM25Doc, BM25Index, rrf_merge
    from tools.storage.vectorstore import VectorStore

    passages_by_id: dict[str, dict] = {}
    ranked_lists: list[list[str]] = []
    # Uploaded text chunks are keyed by raw attachment id, while global element
    # rows use the collision-proof ``upload:<id>`` namespace.
    chunk_scope = paper_id[7:] if paper_id and paper_id.startswith("upload:") else paper_id

    # --- vector track ---
    try:
        vs = VectorStore(storage_context=session.storage_context)
        if vs.available():
            hits = []
            if chunk_scope:
                hits = vs.search_chunks(query, session_id=session.session_id,
                                        paper_id=chunk_scope, top_k=top_k * 3)
            else:
                hits = vs.search_chunks(query, session_id=session.session_id,
                                        top_k=top_k * 3)
                if not hits:
                    hits = vs.search_summaries(query, session_id=session.session_id,
                                               top_k=top_k * 3)
            v_ids = []
            prefix = f"{session.session_id}::"
            for h in hits:
                pid_key = h.id[len(prefix):] if h.id.startswith(prefix) else h.id
                meta = h.metadata or {}
                # Small-to-big: dedupe on the parent passage, return parent text.
                parent_id = meta.get("parent_id") or ""
                key = f"{meta.get('paper_id', '')}::{parent_id}" if parent_id else pid_key
                if key in passages_by_id:
                    continue
                passages_by_id[key] = {
                    "paper_id": meta.get("paper_id", ""),
                    "title": meta.get("title", ""),
                    "section": meta.get("section", ""),
                    "text": meta.get("parent_text") or h.document,
                }
                v_ids.append(key)
            if v_ids:
                ranked_lists.append(v_ids)
    except Exception as e:  # noqa: BLE001
        logger.debug("vector track unavailable: %s", e)

    # --- elements vector track (GLOBAL elements collection, scoped by paper set) ---
    # Elements (figures/tables/formulas) carry VLM understanding text that text
    # chunks lack; a "what does the loss function look like" query lands here.
    # The paper_id scope (not session_id) is what preserves session isolation
    # over this global collection. Kind filter applies only when the query
    # signaled a specific modality.
    try:
        vs = VectorStore(storage_context=session.storage_context)
        if vs.available():
            from tools.ingest.attachments import session_element_scope
            el_scope = [paper_id] if paper_id else session_element_scope(session)
            if el_scope:
                el_hits = vs.search_elements(query, el_scope, kind=modality, top_k=top_k * 2)
                e_ids: list[str] = []
                for h in el_hits:
                    meta = h.metadata or {}
                    eid = meta.get("element_id") or h.id
                    key = _element_passage_id(eid)
                    if key in passages_by_id:
                        continue
                    passages_by_id[key] = {
                        "paper_id": meta.get("paper_id", ""),
                        "title": meta.get("title", ""),
                        "section": meta.get("kind", "") or meta.get("section", ""),
                        "text": h.document,
                        "element_id": eid,
                        "kind": meta.get("kind", ""),
                        "page": meta.get("page", ""),
                    }
                    e_ids.append(key)
                if e_ids:
                    ranked_lists.append(e_ids)
    except Exception as e:  # noqa: BLE001
        logger.debug("elements vector track unavailable: %s", e)

    # --- BM25 track ---
    corpus = [c for c in _session_corpus(session)
              if not paper_id or c["paper_id"] in {paper_id, chunk_scope}]
    if corpus:
        index = BM25Index([BM25Doc(id=c["id"], text=c["text"]) for c in corpus])
        b_ids = []
        for doc, _score in index.search(query, top_k=top_k * 3):
            rec = next(c for c in corpus if c["id"] == doc.id)
            passages_by_id.setdefault(doc.id, {
                "paper_id": rec["paper_id"], "title": rec["title"],
                "section": rec["section"], "text": rec["text"],
            })
            b_ids.append(doc.id)
        if b_ids:
            ranked_lists.append(b_ids)

    if not ranked_lists:
        return []
    fused = rrf_merge(ranked_lists, top_n=top_k)
    return [passages_by_id[k] for k in fused if k in passages_by_id]


def _passages_to_sources(passages: list[dict]) -> list[dict]:
    """Dedupe passages into compact source records (id + title + sections)."""
    by_id: dict[str, dict] = {}
    for p in passages:
        pid = p.get("paper_id", "")
        if not pid:
            continue
        rec = by_id.setdefault(pid, {"paper_id": pid, "title": p.get("title", ""),
                                     "sections": [], "snippet": p.get("text", "")[:160]})
        sec = p.get("section", "")
        if sec and sec not in rec["sections"]:
            rec["sections"].append(sec)
    return list(by_id.values())


# ---------------------------------------------------------------------------
# research_map / reading_path / write_review
# ---------------------------------------------------------------------------

async def _tool_research_map(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    import hashlib
    import json
    from agents.map_agent import build_research_map
    from core.prompts.registry import get as get_prompt
    from core.runtime_performance_policy import get_performance_policy

    if not session.has_papers():
        return err("research_map", ErrorCode.NO_PAPERS,
                   "还没有检索到论文。请先调用 search_papers。")

    _sync_fulltext_status_from_summaries(session)
    policy = get_performance_policy()
    citation_mode = policy.map_citation_mode
    try:
        from core.paper_search_settings_store import get_paper_search_policy
        if not get_paper_search_policy().sources.get("openalex", False):
            citation_mode = "off"
    except Exception:
        citation_mode = "off"

    all_papers = session.all_papers()
    fingerprint_payload = [{
        "id": p.id, "title": p.title, "year": p.year,
        "citation_count": p.citation_count or 0,
        "fulltext_status": getattr(p, "fulltext_status", "unknown"),
        "doi": p.doi or "",
    } for p in all_papers]
    fingerprint_payload.extend([citation_mode, get_prompt("map.summary").version])
    fingerprint = hashlib.sha256(json.dumps(
        fingerprint_payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    cached = session.map_data if isinstance(session.map_data, dict) else {}
    if cached.get("fingerprint") == fingerprint:
        data = dict(cached)
        data["cache_hit"] = True
        session.map_data = data
        return ok("research_map", "研究地图已从当前论文与策略指纹缓存复用。", **data)

    map_data = await build_research_map(
        session.papers, session.candidates, session.sub_directions, session.language,
        progress=progress_cb, citation_mode=citation_mode)
    map_data["fingerprint"] = fingerprint
    map_data["cache_hit"] = False
    session.map_data = map_data

    graph = map_data.get("graph", {})
    n_cite = sum(1 for edge in graph.get("edges", []) if edge.get("type") == "cites")
    n_clusters = len(map_data.get("clusters", []))
    degraded = bool(map_data.get("degraded"))
    return ok(
        "research_map",
        f"研究地图已生成：{n_clusters} 个主题簇，谱系图 {len(graph.get('nodes', []))} 节点 / "
        f"{len(graph.get('edges', []))} 条边（真实引用边 {n_cite} 条）。"
        + ("增强步骤部分降级，但地图仍可用。" if degraded else ""),
        **map_data,
    )


async def _tool_reading_path(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    from agents.map_agent import build_reading_path

    if not session.has_papers():
        return err("reading_path", ErrorCode.NO_PAPERS,
                   "还没有检索到论文。请先调用 search_papers。")

    path = await build_reading_path(session.papers + session.candidates,
                                    session.map_data, session.language,
                                    progress=progress_cb)
    session.reading_path = path
    if not path:
        return partial_result("reading_path",
                              "无法生成阅读路径：核心集为空。请先 search_papers。", path=[])
    return ok(
        "reading_path",
        f"推荐阅读路径（{len(path)} 篇）："
        + " → ".join(f"[{p['role']}]{p['title'][:30]}" for p in path),
        path=path,
    )


async def _tool_write_review(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    from agents.review_agent import review_agent

    if not session.has_papers():
        return err("write_review", ErrorCode.NO_PAPERS,
                   "还没有检索到论文。请先调用 search_papers。")

    # Clusters come from the research map when available; otherwise one group.
    clusters_dict: dict = {}
    for c in (session.map_data or {}).get("clusters", []):
        clusters_dict[str(c.get("id", 0))] = {
            "label": c.get("label", ""), "paper_ids": c.get("paper_ids", []),
        }
    if not clusters_dict:
        clusters_dict = {"0": {"label": "全部论文", "paper_ids": [p.id for p in session.papers]}}

    state: dict = {
        "topic": session.topic,
        "user_conception": session.conception,
        "language": session.language,
        "papers": session.papers,
        "paper_summaries": session.paper_summaries,
        "citation_graph_data": {"clusters": clusters_dict},
        "review_rounds": 0,
        "review_cluster_count": len(clusters_dict),
    }
    result = await review_agent(state, progress_callback=progress_cb)
    session.literature_review = result.get("literature_review", "")

    return ok(
        "write_review",
        f"文献综述已生成（{len(session.literature_review)} 字符，"
        f"按 {len(clusters_dict)} 个主题簇组织）。",
        literature_review=session.literature_review,
        review_chars=len(session.literature_review),
    )


# ---------------------------------------------------------------------------
# Skill layer: use_skill (instruction skills) + executable export skills
# ---------------------------------------------------------------------------

async def _tool_use_skill(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    from core.prompts.registry import get as get_prompt
    from core.skills import all_skills, get_skill

    name = (args.get("name") or "").strip()
    focus = (args.get("focus") or "").strip()
    sk = get_skill(name)
    if sk is None:
        available = ", ".join(s.name for s in all_skills()) or "（无）"
        return err("use_skill", ErrorCode.NO_TOOL,
                   f"未知技能 {name!r}。可用技能：{available}。"
                   "若用户请求不命中任何技能场景，直接用基础工具或直接回答。")
    unmet = _unmet_requires(sk.requires, session)
    if unmet:
        return err("use_skill", ErrorCode.NO_PAPERS, unmet)
    if name in session.loaded_skills:
        return ok("use_skill",
                  f"技能 {name} 的指令本会话已加载（见上文），直接按指令继续执行，不要重复加载。",
                  skill=name, instructions="")
    session.loaded_skills.add(name)
    instructions = get_prompt(f"skill.{name}").text
    extras: list[str] = []
    if focus:
        extras.append(f"用户焦点：{focus}（围绕焦点裁剪执行范围）")
    if sk.output_check:
        extras.append(f"完成前自检：{sk.output_check}")
    extras.append("预算提醒：本回合工具调用上限已放宽；定向问答按技能规定的次数执行，不要超额补查。")
    return ok("use_skill",
              f"技能 {name} 已加载。严格按以下工作流指令执行：",
              skill=name,
              instructions=instructions + "\n\n---\n" + "\n".join(extras))


def _unmet_requires(requires: tuple[str, ...], session: ChatSession) -> str | None:
    """Declarative skill preconditions; returns an embedded-guidance message."""
    checks = {
        "papers": (session.has_papers(), "先 search_papers 检索论文"),
        "map": (bool(session.map_data), "先 research_map 生成研究地图"),
        "review": (bool(session.literature_review), "先 write_review 撰写综述"),
        "attachments": (bool(session.attachments), "先让用户上传文件（如草稿 PDF）"),
    }
    for r in requires:
        satisfied, guidance = checks[r]
        if not satisfied:
            return f"技能前置条件未满足（{r}）：{guidance}，完成后再加载该技能。"
    return None


async def _tool_citation_export(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    from tools.export.bibtex import papers_to_bibtex
    from tools.export.enrich import enrich_by_dois
    from tools.export.gbt7714 import papers_to_gbt7714

    if not session.has_papers():
        return err("citation_export", ErrorCode.NO_PAPERS,
                   "还没有可导出的论文。请先 search_papers 检索。")
    format_ = args.get("format") or "bibtex"
    paper_ids = [pid for pid in (args.get("paper_ids") or []) if pid]
    # Default scope is the CORE set — the full pool (core+candidates)
    # surprised users with 25 entries when they asked for "these papers".
    selected = (_select_session_papers(session, paper_ids)
                if paper_ids else list(session.papers))
    if not selected:
        return err("citation_export", ErrorCode.VALIDATION_ERROR,
                   "指定的 paper_ids 不在当前论文集中。请从 search_papers 结果的 id 中选择。")

    # Best-effort metadata enrichment (volume/issue/pages) from Crossref.
    extras: dict[str, dict] = {}
    dois = [p.doi for p in selected if p.doi]
    if dois:
        if progress_cb:
            progress_cb("正在经 Crossref 补全引用元数据...")
        try:
            extras = await enrich_by_dois(dois)
        except Exception:  # noqa: BLE001
            extras = {}

    if format_ == "gbt7714":
        text = papers_to_gbt7714(selected, extras)
        fmt_label = "GB/T 7714"
    else:
        text = papers_to_bibtex(selected, extras)
        fmt_label = "BibTeX"
    enriched = sum(1 for p in selected if p.doi and p.doi in extras)
    msg = f"已导出 {len(selected)} 篇论文的 {fmt_label} 引用"
    if enriched:
        msg += f"（{enriched} 篇经 Crossref 补全卷期页）"
    return ok("citation_export", msg + "。",
              citations=text, format=format_, count=len(selected))


async def _tool_export_report(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    from tools.export.report import write_reports
    kind = args.get("kind") or "all"
    if kind == "research_map" and not session.map_data:
        return err("export_report", ErrorCode.NO_PAPERS,
                   "还没有研究地图可导出。请先 research_map 生成。")
    if kind == "write_review" and not session.literature_review:
        return err("export_report", ErrorCode.NO_PAPERS,
                   "还没有文献综述可导出。请先 write_review 撰写。")
    kinds = {"research_map", "write_review"} if kind == "all" else {kind}
    files = write_reports(session, kinds)
    if not files:
        return partial_result(
            "export_report",
            "没有可导出的内容：研究地图报告需先 research_map，综述文件需先 write_review。",
            files=[],
        )
    for f in files:
        f.setdefault("url", f"/files/{f['fileName']}")
    names = "、".join(f.get("displayName") or f["fileName"] for f in files)
    return ok("export_report",
              f"已导出 {len(files)} 个报告文件：{names}。把下载链接发给用户。",
              files=files)


def _load_attachment_text(session: ChatSession, target: str, tool: str):
    """Pick an attachment (id/filename fragment; empty = most recent) and read
    its extracted text. Returns (att_dict, text) or (None, ToolResult error)."""
    from tools.ingest.attachments import attachment_text_path

    atts = session.attachments or []
    if not atts:
        return None, err(tool, ErrorCode.NO_PAPERS,
                         "会话中还没有上传的文件。请先让用户上传论文草稿"
                         "（PDF/DOCX/TEX/TXT/MD），然后再调用本工具。")
    target = (target or "").strip().lower()
    if target:
        att = next((a for a in atts
                    if target in str(a.get("id", "")).lower()
                    or target in str(a.get("filename", "")).lower()), None)
        if att is None:
            names = "、".join(str(a.get("filename", a.get("id", "?"))) for a in atts)
            return None, err(tool, ErrorCode.VALIDATION_ERROR,
                             f"没有找到匹配的附件 {target!r}。当前附件：{names}。")
    else:
        att = atts[-1]  # most recent upload by default

    aid = att.get("id")
    try:
        sidecar = attachment_text_path(att, session)
        text = sidecar.read_text(encoding="utf-8") if sidecar and sidecar.is_file() else ""
    except OSError:
        return None, err(tool, ErrorCode.NO_PAPERS,
                         f"附件 {att.get('filename', aid)} 的文本内容不可用"
                         "（可能未成功解析）。请让用户重新上传。")
    if len(text.strip()) < 200:
        return None, err(tool, ErrorCode.VALIDATION_ERROR,
                         "附件文本过短（<200 字符），不像一篇论文草稿。"
                         "请确认上传了正确的文件。")
    return att, text


async def _tool_check_structure(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    """Deterministic draft structure check — pure parsing, no LLM calls."""
    from tools.writing.structure_check import analyze_draft

    att, result_or_err = _load_attachment_text(
        session, args.get("attachment") or "", "check_structure")
    if att is None:
        return result_or_err
    text = result_or_err

    result = analyze_draft(text, filename=str(att.get("filename", att.get("id"))))
    n_high = sum(1 for i in result["issues"] if i["severity"] == "high")
    n_mid = sum(1 for i in result["issues"] if i["severity"] == "medium")
    n_low = sum(1 for i in result["issues"] if i["severity"] == "low")
    return ok(
        "check_structure",
        f"结构体检完成：识别 {result['metrics']['section_count']} 个章节，"
        f"缺失 {len(result['missing_sections'])} 个标准章节，"
        f"发现 {len(result['issues'])} 个问题（高 {n_high} / 中 {n_mid} / 低 {n_low}）。"
        "以下是确定性体检事实；请基于它逐条给用户解读、判断阈值误报并给出可执行的修改建议"
        "（定性逐节评审可加载 draft_review 技能继续）。",
        **result,
    )


async def _tool_check_format(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    """Deterministic format check — pure parsing, no LLM calls."""
    from tools.writing.format_check import analyze_format

    att, result_or_err = _load_attachment_text(
        session, args.get("attachment") or "", "check_format")
    if att is None:
        return result_or_err
    text = result_or_err
    spec = (args.get("spec") or "").strip()

    result = analyze_format(text, filename=str(att.get("filename", att.get("id"))),
                            spec=spec)
    n_high = sum(1 for i in result["issues"] if i["severity"] == "high")
    n_spec_bad = sum(1 for r in result["spec_results"] if not r["ok"])
    return ok(
        "check_format",
        f"格式检查完成（{'LaTeX' if result['is_latex'] else '文本'}模式）："
        f"发现 {len(result['issues'])} 个问题（高危 {n_high} 个）"
        + (f"；格式要求对照 {len(result['spec_results'])} 项，{n_spec_bad} 项不达标" if spec else "")
        + (f"；{len(result['layout_manual_checks'])} 项排版要求需人工在 Word/LaTeX 核对"
           if result["layout_manual_checks"] else "")
        + "。以下是确定性检查事实；请逐条给用户解读并给出可执行的修改建议"
        "（对照用户格式要求的完整流程可加载 format_compliance 技能）。",
        **result,
    )


async def _tool_export_manuscript(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    """Export writing output (draft/polish/fix-list) as md/docx/tex download."""
    from tools.writing.manuscript_export import export_manuscript

    title = (args.get("title") or "").strip() or "论文文稿"
    content = (args.get("content") or "").strip()
    if len(content) < 50:
        return err("export_manuscript", ErrorCode.VALIDATION_ERROR,
                   "导出内容过短（<50 字符）。请先把完整文稿写好，再整体导出。")
    if len(content) > 100_000:
        return err("export_manuscript", ErrorCode.VALIDATION_ERROR,
                   "导出内容过长（>10 万字符）。请拆分章节分别导出。")
    fmt = args.get("format") or "docx"
    rec = export_manuscript(
        title, content, fmt, storage_context=session.storage_context,
        session_id=session.session_id, owner_id=getattr(session, "owner_id", ""),
    )
    rec.setdefault("url", f"/files/{rec['fileName']}")
    shown_name = rec.get("displayName") or rec["fileName"]
    return ok("export_manuscript",
              f"已导出 {fmt} 文件：{shown_name}（{rec['size']} 字节）。"
              "把下载链接发给用户。",
              files=[rec], format=fmt)


# ---------------------------------------------------------------------------
# integrity_sweep (deterministic retraction / erratum / preprint→published check)
# ---------------------------------------------------------------------------

async def _tool_integrity_sweep(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    """Official-API integrity check — zero LLM. Retraction / erratum / preprint→published."""
    from tools.search.integrity import sweep as integrity_sweep

    paper_ids = [pid for pid in (args.get("paper_ids") or []) if pid]
    selected = (_select_session_papers(session, paper_ids)
                if paper_ids else session.all_papers())
    if not selected:
        if paper_ids:
            return err("integrity_sweep", ErrorCode.VALIDATION_ERROR,
                       "指定的 paper_ids 不在当前论文集中。请从 search_papers 结果的 id 中选择。")
        return err("integrity_sweep", ErrorCode.NO_PAPERS,
                   "还没有论文可质检。请先 search_papers。")

    if progress_cb:
        progress_cb(f"正在对 {len(selected)} 篇论文做可靠性质检（OpenAlex/Crossref/arXiv）...")
    result = await integrity_sweep(selected)
    facts = result["facts"]
    n_retract = len(facts.get("retracted", []))
    n_concern = len(facts.get("concern", []))
    n_preprint = len(facts.get("preprint_published", []))
    n_unknown = len(facts.get("unknown", []))
    n_clean = len(facts.get("clean", []))
    msg = (f"可靠性质检完成（{len(selected)} 篇）：✅ 无异常 {n_clean} · "
           f"⛔ 撤稿 {n_retract} · ⚠️ 勘误/关切 {n_concern} · "
           f"🔁 预印本已正式发表 {n_preprint} · ❓ 未查到 {n_unknown}。")
    if n_retract or n_concern:
        msg += "存在风险的篇目请在引用前核实，撤稿论文一般不应引用。"
    return ok("integrity_sweep", msg, report=result["report"], facts=facts)


# ---------------------------------------------------------------------------
# bib_import (deterministic .bib parse + DOI enrichment + merge into candidates)
# ---------------------------------------------------------------------------

def _load_bib_attachment(session: ChatSession, target: str):
    """Pick a .bib attachment (id/filename fragment; empty = most recent) and
    read its extracted text. Unlike _load_attachment_text, no draft-length
    gate — a .bib with a single short entry is still valid. Returns (text|None, ToolResult|None)."""
    from tools.ingest.attachments import attachment_text_path

    atts = session.attachments or []
    if not atts:
        return None, err("bib_import", ErrorCode.NO_PAPERS,
                         "会话中还没有上传的文件。请先让用户上传 .bib 文献库"
                         "（Zotero/EndNote/Mendeley 导出），再调用本工具。")
    target = (target or "").strip().lower()
    candidates = [a for a in atts
                  if str(a.get("filename", "")).lower().endswith(".bib")] or atts
    if target:
        att = next((a for a in candidates
                    if target in str(a.get("id", "")).lower()
                    or target in str(a.get("filename", "")).lower()), None)
        if att is None:
            names = "、".join(str(a.get("filename", a.get("id", "?"))) for a in candidates)
            return None, err("bib_import", ErrorCode.VALIDATION_ERROR,
                             f"没有找到匹配的 .bib 附件 {target!r}。当前附件：{names}。")
    else:
        att = candidates[-1]

    aid = att.get("id")
    try:
        sidecar = attachment_text_path(att, session)
        text = sidecar.read_text(encoding="utf-8") if sidecar and sidecar.is_file() else ""
    except OSError:
        return None, err("bib_import", ErrorCode.NO_PAPERS,
                         f"附件 {att.get('filename', aid)} 的文本内容不可用"
                         "（可能未成功解析）。请让用户重新上传。")
    if not text.strip():
        return None, err("bib_import", ErrorCode.VALIDATION_ERROR,
                         "附件内容为空，没有可导入的 BibTeX 条目。")
    return text, None


async def _tool_bib_import(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    """Parse uploaded .bib → enrich DOIs → dedup-merge into candidates. Zero LLM."""
    from tools.export.bibtex_import import parse_bibtex
    from tools.search.base import generate_paper_id, normalize_title, title_similarity

    text, err_res = _load_bib_attachment(session, args.get("attachment") or "")
    if err_res is not None:
        return err_res

    entries = parse_bibtex(text)
    if not entries:
        return err("bib_import", ErrorCode.VALIDATION_ERROR,
                   "未能从附件解析出任何 BibTeX 条目。请确认上传的是 .bib 文件"
                   "（Zotero/EndNote/Mendeley 的 BibTeX 导出）。")

    # Best-effort DOI enrichment (volume/issue/venue) — reuse citation path.
    dois = [e["doi"] for e in entries if e["doi"]]
    extras: dict[str, dict] = {}
    if dois:
        if progress_cb:
            progress_cb(f"正在经 Crossref 补全 {len(dois)} 条 DOI 元数据...")
        try:
            from tools.export.enrich import enrich_by_dois
            extras = await enrich_by_dois(dois)
        except Exception:  # noqa: BLE001
            extras = {}

    existing_ids = {p.id for p in session.all_papers()}
    existing_titles = [normalize_title(p.title) for p in session.all_papers() if p.title]

    imported: list[dict] = []
    imported_papers: list[Paper] = []
    failures: list[dict] = []
    enriched = 0
    for e in entries:
        title = e["title"].strip()
        key = e.get("key") or ""
        if not title:
            failures.append({"key": key, "reason": "缺少 title 字段"})
            continue
        doi = (e["doi"] or "").strip() or None
        first_author = e["authors"][0] if e["authors"] else ""
        pid = generate_paper_id(title, first_author, e["year"], doi)
        # Dedup against the existing corpus: id/DOI equality, then title Jaccard ≥0.95.
        if pid in existing_ids:
            failures.append({"key": key, "reason": "已在会话中（DOI/id 命中），跳过"})
            continue
        ntitle = normalize_title(title)
        if any(title_similarity(ntitle, t) >= 0.95 for t in existing_titles):
            failures.append({"key": key, "reason": "与会话已有论文标题高度重复，跳过"})
            continue
        extra = extras.get(doi, {}) if doi else {}
        venue = e["venue"] or extra.get("venue", "")
        paper = Paper(id=pid, title=title, authors=e["authors"], year=e["year"],
                      doi=doi, venue=venue, source="bibtex", language="en",
                      abstract=e["abstract"],
                      urls=({"doi": f"https://doi.org/{doi}"} if doi else {}))
        if doi and extra:
            enriched += 1
        session.candidates.append(paper)
        existing_ids.add(pid)
        existing_titles.append(ntitle)
        imported.append({"id": pid, "title": title, "doi": doi or "",
                         "verified": bool(doi)})
        imported_papers.append(paper)

    if imported_papers:
        def _index_imported() -> None:
            try:
                from tools.storage.vectorstore import VectorStore

                vs = VectorStore(storage_context=session.storage_context)
                if vs.available():
                    for imported_paper in imported_papers:
                        vs.upsert_paper_summary(
                            imported_paper, None, session_id=session.session_id
                        )
            except Exception:  # noqa: BLE001
                pass

        await run_cpu_bound(_index_imported)

    msg = f"已导入 {len(imported)} 篇"
    if enriched:
        msg += f"（{enriched} 篇经 Crossref 补全元数据）"
    if failures:
        msg += f"，{len(failures)} 条跳过"
    msg += "。导入的论文已并入候选集，可对其 deep_read / write_review / citation_export。"
    return ok("bib_import", msg,
              report={"imported": len(imported), "enriched": enriched,
                      "failed": len(failures), "failures": failures},
              entries=imported)


# ---------------------------------------------------------------------------
# exhibit_index (deterministic figure/table caption extraction, zero LLM)
# ---------------------------------------------------------------------------

def _captions_for(paper: Paper, session: ChatSession) -> list[dict]:
    """Page-accurate (PDF) → text (full_text) caption extraction, best-effort."""
    from pathlib import Path

    from core.reading_policy import is_full_text
    from tools.pdf.caption import extract_captions, extract_captions_from_pdf

    pdf_path = getattr(paper, "pdf_path", None)
    if pdf_path and Path(pdf_path).exists():
        caps = extract_captions_from_pdf(pdf_path)
        if caps:
            return caps
    summary = session.paper_summaries.get(paper.id)
    full_text = getattr(summary, "full_text", None) if summary else None
    if is_full_text(full_text):
        return extract_captions(full_text)
    return []


async def _tool_exhibit_index(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    """List paper captions plus persisted multimodal elements from uploads."""
    from core.config import get_settings
    from tools.ingest.attachments import (
        attachment_by_id, attachment_document_id, ensure_attachment_understood,
    )
    from tools.storage.database import Database

    paper_ids = [pid for pid in (args.get("paper_ids") or []) if pid]
    attachment_ids = [aid for aid in (args.get("attachment_ids") or []) if aid]
    pool = (_select_session_papers(session, paper_ids)
            if paper_ids else ([] if attachment_ids else session.all_papers()))
    if pool is None:
        return err("exhibit_index", ErrorCode.VALIDATION_ERROR,
                   "部分 paper_ids 不在当前会话。")

    if attachment_ids:
        attachments = [attachment_by_id(session, aid) for aid in attachment_ids]
        if any(a is None for a in attachments):
            return err("exhibit_index", ErrorCode.VALIDATION_ERROR,
                       "部分 attachment_ids 不属于当前会话。")
        attachments = [a for a in attachments if a is not None]
    elif not paper_ids:
        attachments = list(session.attachments or [])
    else:
        attachments = []
    if not pool and not attachments:
        return err("exhibit_index", ErrorCode.NO_PAPERS,
                   "当前会话没有论文或上传附件。")

    exhibits: list[dict] = []
    missing: list[dict] = []
    attachment_results: list[dict] = []
    for paper in pool:
        caps = _captions_for(paper, session)
        if caps:
            exhibits.append({"paper_id": paper.id, "title": paper.title, "captions": caps})
        elif paper_ids:
            missing.append({"id": paper.id, "title": paper.title})

    db = Database(storage_context=session.storage_context) if session.storage_context is not None else Database(get_settings().storage.sqlite_path)
    try:
        for attachment in attachments:
            if progress_cb:
                progress_cb(f"正在按需解析上传文件图表：{attachment.get('filename', '')[:40]}...")
            understood = await ensure_attachment_understood(attachment, session, focus="图表导览")
            attachment_results.append({"filename": attachment.get("filename", ""), **understood})
            doc_id = attachment_document_id(attachment["id"])
            rows = db.get_elements(doc_id)
            if rows:
                exhibits.append({
                    "paper_id": doc_id, "attachment_id": attachment["id"],
                    "title": attachment.get("filename", attachment["id"]),
                    "captions": [{
                        "element_id": row.get("element_id"), "type": row.get("kind"),
                        "kind": row.get("kind"), "number": row.get("ordinal"),
                        "caption": row.get("caption") or "", "page": row.get("page") or 0,
                        "asset_url": _element_asset_url(doc_id, row.get("asset_path")),
                    } for row in rows],
                })
            else:
                missing.append({"id": attachment["id"],
                                "title": attachment.get("filename", attachment["id"])})
    finally:
        db.close()

    total = sum(len(group["captions"]) for group in exhibits)
    if total == 0:
        return partial_result(
            "exhibit_index",
            "未提取到图、表或公式；文本仍可正常问答，扫描件在未配置 VLM 时会自动降级。",
            exhibits=[], missing_fulltext=missing, attachments=attachment_results)
    return ok("exhibit_index",
              f"图表导览完成：从 {len(exhibits)} 个文档提取 {total} 个元素。",
              exhibits=exhibits, missing_fulltext=missing,
              attachments=attachment_results)


# ---------------------------------------------------------------------------
# explain_element (drill into one figure/table/formula — VLM understanding)
# ---------------------------------------------------------------------------

_ELEMENT_KIND_LABEL = {"figure": "图", "table": "表", "formula": "公式"}


def _element_asset_url(paper_id: str, asset_path: str | None) -> str | None:
    """Relative frontend URL for an element's crop (matches the
    /api/v1/elements/assets/{paper_id}/{filename} route). None when no crop.

    paper_id is sanitized the same way the structure parser names the on-disk
    asset dir (tools.pdf.fetcher._sanitize_filename_component), so DOIs /
    arXiv ids with ':' and '/' resolve to the right directory.
    """
    if not asset_path:
        return None
    from pathlib import Path
    from tools.pdf.fetcher import _sanitize_filename_component as _sanitize
    fname = Path(asset_path).name
    if not fname:
        return None
    return f"/api/v1/elements/assets/{_sanitize(paper_id)}/{fname}"


async def _tool_explain_element(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    """Return the full multimodal detail of one figure/table/formula element."""
    from core.config import get_settings
    from tools.storage.database import Database

    element_id = (args.get("element_id") or "").strip()
    if not element_id:
        return err("explain_element", ErrorCode.VALIDATION_ERROR,
                   "缺少必填参数 element_id（形如 `{paper_id}::figure::3`）。")
    paper_id = (args.get("paper_id") or "").strip()
    if not paper_id:
        # element_id = "{paper_id}::{kind}::{ordinal}"; rsplit so a paper_id
        # that itself contains '::' (unlikely) still parses correctly.
        parts = element_id.rsplit("::", 2)
        if len(parts) == 3:
            paper_id = parts[0]
    if not paper_id:
        return err("explain_element", ErrorCode.VALIDATION_ERROR,
                   "无法从 element_id 解析出 paper_id，请显式传入 paper_id。")

    # Session isolation covers both network papers and collision-proof upload
    # namespaces.  Never authorize an upload merely because its global row exists.
    from tools.ingest.attachments import (
        attachment_by_id, ensure_attachment_understood, session_element_scope,
    )
    if paper_id not in set(session_element_scope(session)):
        return err("explain_element", ErrorCode.NO_PAPERS,
                   f"文档 {paper_id} 不在当前会话中，无法解读其元素。")
    if paper_id.startswith("upload:"):
        attachment = attachment_by_id(session, paper_id[7:])
        if attachment is not None and attachment.get("multimodal_status") != "ready":
            await ensure_attachment_understood(attachment, session, focus=element_id)

    db = Database(storage_context=session.storage_context) if session.storage_context is not None else Database(get_settings().storage.sqlite_path)
    try:
        rows = db.get_elements(paper_id)
    finally:
        db.close()
    row = next((r for r in rows if r.get("element_id") == element_id), None)
    if row is None:
        return partial_result(
            "explain_element",
            f"未找到元素 {element_id}。该篇可能尚未 deep_read，或元素 id 有误。"
            "可先 exhibit_index 列出可用元素，或 deep_read 获取全文与图表。",
            element=None)

    kind = row.get("kind") or ""
    element = {
        "element_id": element_id,
        "paper_id": paper_id,
        "kind": kind,
        "page": row.get("page"),
        "section": row.get("section") or "",
        "caption": row.get("caption") or "",
        "understanding": row.get("understanding") or {},
        "docling_extract": row.get("docling_extract") or {},
        "asset_url": _element_asset_url(paper_id, row.get("asset_path")),
    }
    label = _ELEMENT_KIND_LABEL.get(kind, "元素")
    cap = (row.get("caption") or "").strip()
    msg = f"{label}解读完成：{element_id}" + (f"（{cap[:40]}）" if cap else "")
    return ok("explain_element", msg, element=element)


# ---------------------------------------------------------------------------
# field_census (OpenAlex group_by aggregation + 1 utility portrait)
# ---------------------------------------------------------------------------

async def _tool_field_census(args: dict, session: ChatSession, progress_cb) -> ToolResult:
    """Macro census of the field — distinct from research_map (YOUR papers' structure)."""
    from tools.search.openalex_census import fetch_census, portrait

    if not session.search_queries and not session.topic:
        return err("field_census", ErrorCode.NO_PAPERS,
                   "还没有检索式或主题。请先 search_papers 检索，再用本工具看整个领域的宏观画像。")
    query = (session.search_queries[0] if session.search_queries else session.topic)

    if progress_cb:
        progress_cb("正在聚合 OpenAlex 领域计量数据（年度趋势/作者/机构/期刊）...")
    census = await fetch_census(query)
    portrait_text = await portrait(query, census)

    has_data = any(census.values())
    if not has_data:
        return partial_result(
            "field_census",
            f"OpenAlex 未能为检索式「{query[:40]}」聚合出数据（可能检索式过窄或网络受限）。"
            "可换更宽泛的主题词后重试。",
            yearly=[], top_authors=[], top_institutions=[], top_venues=[], portrait="")

    n_years = len(census["yearly"])
    msg = (f"领域普查完成（检索式：{query[:40]}）：{n_years} 个年度数据点，"
           f"高产作者/机构/期刊各 top {len(census['top_authors'])}。")
    if portrait_text:
        msg += "附 3 句领域画像。"
    else:
        msg += "（画像生成失败，见下表）"
    return ok("field_census", msg,
              yearly=census["yearly"], top_authors=census["top_authors"],
              top_institutions=census["top_institutions"],
              top_venues=census["top_venues"], portrait=portrait_text)


_IMPLS = {
    "search_papers": _tool_search_papers,
    "deep_read": _tool_deep_read,
    "ask_papers": _tool_ask_papers,
    "research_map": _tool_research_map,
    "reading_path": _tool_reading_path,
    "write_review": _tool_write_review,
    "use_skill": _tool_use_skill,
    "citation_export": _tool_citation_export,
    "export_report": _tool_export_report,
    "check_structure": _tool_check_structure,
    "check_format": _tool_check_format,
    "export_manuscript": _tool_export_manuscript,
    "integrity_sweep": _tool_integrity_sweep,
    "bib_import": _tool_bib_import,
    "exhibit_index": _tool_exhibit_index,
    "explain_element": _tool_explain_element,
    "field_census": _tool_field_census,
}


# ---------------------------------------------------------------------------
# Skill surfacing (data-driven hints appended to tool results)
# ---------------------------------------------------------------------------

_SKILL_HINTS: dict[str, tuple[str, ...]] = {
    "search_papers": ("compare_papers", "research_gap", "citation_export", "topic_advisor"),
    "research_map": ("research_gap", "export_report"),
    "deep_read": ("paper_critique", "presentation_prep"),
    "write_review": ("export_report", "citation_export"),
    "check_structure": ("draft_review", "evidence_anchor"),
    "check_format": ("format_compliance",),
}


def suggest_skills(tool_name: str, session: ChatSession) -> str | None:
    """Name the skills whose trigger scenes a successful tool just unlocked.

    The metadata section in the system prompt fades from recency as tool
    results pile up; re-surfacing the relevant skill names right inside the
    tool result is what keeps dispatch recall high. Registry-checked so
    hints never point at uninstalled skills.
    """
    from core.skills import get_skill

    names = [n for n in _SKILL_HINTS.get(tool_name, ())
             if get_skill(n) is not None and n not in session.loaded_skills]
    if not names:
        return None
    return ("后续若命中场景可加载技能：" + "、".join(names)
            + "（use_skill 加载；不命中就不要加载）。")


# ---------------------------------------------------------------------------
# Rule reflector (always on; data-driven, no fragile text parsing)
# ---------------------------------------------------------------------------

_REFLECT_RULES: dict[str, list[tuple[Callable[[ToolResult], bool], str]]] = {
    "search_papers": [
        (lambda r: len(r.data.get("core_titles", [])) == 0 and r.data.get("total_papers", 0) > 0,
         "检索到论文但核心集为空：主题可能过宽或过窄，建议调整检索式。"),
    ],
    "research_map": [
        (lambda r: len(r.data.get("clusters", [])) == 0,
         "研究地图没有产生主题簇：论文数量可能太少。"),
    ],
    "write_review": [
        (lambda r: r.data.get("review_chars", 0) < 500,
         "综述过短（<500 字符）：素材可能不足，建议先 deep_read 再重写。"),
    ],
    "reading_path": [
        (lambda r: len(r.data.get("path", [])) == 0,
         "阅读路径为空：缺少可用的核心集论文。"),
    ],
}


def reflect_tool_result(tool_name: str, result: ToolResult) -> dict | None:
    """Run rule-based reflection on a tool result; returns a warning or None."""
    if result.is_error:
        return None
    for check, message in _REFLECT_RULES.get(tool_name, []):
        try:
            if check(result):
                return {"warning": message, "tool": tool_name}
        except Exception:  # noqa: BLE001
            continue
    return None
