"""Search Agent: intent understanding → multi-source retrieval → semantic
rerank → adaptive tiering.

Pipeline (one LLM call total; reranking is local embeddings, zero API cost):

  topic (+conception)
    → ① understand: 1 LLM call → research_goal + sub_directions + queries
        (JSON-tolerant parse, falls back to the raw topic)
    → ② SearchManager.search_all: multi-source parallel search + dedup
    → ③ semantic_rerank: local MiniLM embeddings + citation + recency
        (degrades to token-overlap when the embedder is unavailable)
    → ④ adaptive_tier: threshold + elbow detection → core set / candidates
        (no user-facing X/Y/Z counts)
    → persist SQLite + index session-scoped Level-1 vectors
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
import time
from datetime import datetime

from langchain_core.messages import HumanMessage

from core.blocking import run_cpu_bound
from core.config import get_settings
from core.embeddings import embed_texts
from core.llm import ainvoke_utility, get_llm
from core.models import Paper
from core.prompts.search import get_understand_prompt
from core.state import ResearchState
from tools.retrieval.bm25 import tokenize
from tools.search.manager import SearchManager
from tools.search.router import DISCIPLINES, QUERY_INTENTS
from tools.storage.database import Database

logger = logging.getLogger(__name__)

# Adaptive tiering bounds (deterministic, not user-facing).
_MIN_CORE = 5
_MAX_CORE = 12
_MAX_CANDIDATES = 25
_MIN_FULLTEXT_CORE = 5
_ABS_TAU = 0.35        # absolute score floor (embedding path)
_REL_TAU = 0.45        # relative-to-top1 floor
_ELBOW_DROP = 0.25     # relative score drop that marks the core/candidate cut

# The understand-LLM call is the only stage without its own network budget;
# a stalled provider must not eat the whole tool budget before retrieval
# even starts. On timeout the raw topic is used directly.
_UNDERSTAND_TIMEOUT_SECONDS = 10.0
# Shares of the tool budget reserved for stages after retrieval (rerank is
# CPU-bound; the probe stage clamps itself to whatever remains).
_SEARCH_TAIL_RESERVE_SECONDS = 8.0
_PROBE_TAIL_RESERVE_SECONDS = 3.0
_PROBE_MIN_SECONDS = 2.0


def _lang_instruction(language: str) -> str:
    return {"zh": "中文", "en": "English"}.get(language, "中英双语")


# ---------------------------------------------------------------------------
# ① Intent understanding
# ---------------------------------------------------------------------------

def _fallback_plan(topic: str) -> dict:
    """Trivial plan over the raw topic (understand-LLM failed or timed out)."""
    return {"research_goal": topic,
            "sub_directions": [{"name": topic, "queries_en": [topic], "queries_zh": []}],
            "queries": [topic], "disciplines": [], "query_intents": [],
            "requested_sources": [], "requires_preprints": False, "requires_datasets": False}


def parse_understanding(raw: str, topic: str) -> dict:
    """Tolerantly parse the understand-LLM's JSON output.

    Strips code fences, falls back to the first {...} block, and finally to a
    trivial plan over the raw topic. Always returns
    {research_goal, sub_directions, queries}.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else parts[0]
        if text.startswith("json"):
            text = text[4:]
    data = None
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return _fallback_plan(topic)

    sub_directions = []
    queries: list[str] = []
    for sd in data.get("sub_directions") or []:
        if not isinstance(sd, dict):
            continue
        name = str(sd.get("name", "")).strip()[:80]
        q_en = [str(q).strip() for q in (sd.get("queries_en") or []) if str(q).strip()][:3]
        q_zh = [str(q).strip() for q in (sd.get("queries_zh") or []) if str(q).strip()][:2]
        sub_directions.append({"name": name, "queries_en": q_en, "queries_zh": q_zh})
        queries.extend(q_en)
        queries.extend(q_zh)
    if not queries:
        queries = [topic]
        if not sub_directions:
            sub_directions = [{"name": topic, "queries_en": [topic], "queries_zh": []}]
    from tools.search.registry import SOURCE_IDS
    disciplines = [str(v) for v in (data.get("disciplines") or []) if str(v) in DISCIPLINES][:2]
    intents = [str(v) for v in (data.get("query_intents") or []) if str(v) in QUERY_INTENTS][:3]
    requested = [str(v) for v in (data.get("requested_sources") or []) if str(v) in SOURCE_IDS][:4]
    return {
        "research_goal": str(data.get("research_goal") or topic).strip()[:200],
        "sub_directions": sub_directions, "queries": queries,
        "disciplines": disciplines, "query_intents": intents, "requested_sources": requested,
        "requires_preprints": bool(data.get("requires_preprints")),
        "requires_datasets": bool(data.get("requires_datasets")),
    }


async def _understand(topic: str, conception: str, language: str) -> dict:
    """One LLM call: ground the topic and decompose it into queries."""
    llm = get_llm("light")
    prompt = get_understand_prompt().format(
        topic=topic,
        conception=conception or "（无）",
        language_instruction=_lang_instruction(language),
    )
    try:
        resp = await ainvoke_utility(llm, [HumanMessage(content=prompt)])
        raw = resp.content if hasattr(resp, "content") else str(resp)
    except Exception as e:
        logger.warning("understand LLM call failed: %s", e)
        raw = ""
    return parse_understanding(raw, topic)


# ---------------------------------------------------------------------------
# ③ Semantic rerank (pure, injectable embed_fn for tests)
# ---------------------------------------------------------------------------

def _token_overlap(paper: Paper, query_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    title_tokens = set(tokenize(paper.title or ""))
    if not title_tokens:
        return 0.0
    return len(title_tokens & query_tokens) / len(query_tokens)


def rerank_papers(
    papers: list[Paper],
    query_text: str,
    embed_fn=embed_texts,
) -> list[Paper]:
    """Score + sort papers by semantic relevance.

    score = 0.55*cos(embed) + 0.20*citation + 0.15*recency + 0.10*token_overlap
    Without an embedder the cosine term is replaced by token overlap
    (weights renormalized). Sets paper.relevance_score (0..1) and returns a
    new list sorted desc.
    """
    if not papers:
        return []

    query_tokens = set(tokenize(query_text))
    max_citations = max((p.citation_count or 0) for p in papers) or 1
    current_year = datetime.now().year

    # --- embeddings (one batch: query + all papers) ---
    cos_scores: list[float] | None = None
    if embed_fn is not None:
        try:
            docs = [query_text] + [
                f"{p.title or ''}\n{(p.abstract or '')[:400]}" for p in papers
            ]
            vecs = embed_fn(docs)
            if vecs and len(vecs) == len(docs):
                qv = vecs[0]
                cos_scores = [
                    max(0.0, min(1.0, sum(a * b for a, b in zip(qv, v))))
                    for v in vecs[1:]
                ]
        except Exception as e:  # noqa: BLE001
            logger.warning("rerank embedding failed, degrading to token overlap: %s", e)
            cos_scores = None

    scored: list[Paper] = []
    for i, p in enumerate(papers):
        overlap = _token_overlap(p, query_tokens)
        citation_norm = math.log((p.citation_count or 0) + 1) / math.log(max_citations + 1)
        if p.year:
            recency = max(0.0, 1.0 - max(current_year - p.year, 0) * 0.08)
        else:
            recency = 0.3
        if cos_scores is not None:
            score = 0.55 * cos_scores[i] + 0.20 * citation_norm + 0.15 * recency + 0.10 * overlap
        else:
            score = 0.65 * overlap + 0.20 * citation_norm + 0.15 * recency
        p.relevance_score = round(score, 4)
        scored.append(p)

    scored.sort(key=lambda p: p.relevance_score, reverse=True)
    return scored


# ---------------------------------------------------------------------------
# ④ Adaptive tiering (pure)
# ---------------------------------------------------------------------------

def rebalance_core_for_fulltext(
    core: list[Paper],
    candidates: list[Paper],
    *,
    minimum_available: int = _MIN_FULLTEXT_CORE,
) -> tuple[list[Paper], list[Paper], list[str]]:
    """Best-effort full-text guarantee after verified availability probing.

    Keep every relevance-selected core paper and *promote* the highest-scoring
    verified-available candidates until the core contains up to
    ``minimum_available`` readable papers. Promotions are removed from the
    candidate layer and are never backfilled; unknown/unavailable rows never
    count. The total paper catalogue therefore stays unchanged.
    """
    from core.reading_policy import normalize_fulltext_status

    seen: set[str] = set()
    core_out: list[Paper] = []
    for paper in core:
        if paper.id and paper.id not in seen:
            core_out.append(paper)
            seen.add(paper.id)
    candidate_out: list[Paper] = []
    for paper in candidates:
        if paper.id and paper.id not in seen:
            candidate_out.append(paper)
            seen.add(paper.id)
    available_total = sum(
        normalize_fulltext_status(p.fulltext_status) == "available"
        for p in core_out + candidate_out
    )
    target = min(max(0, minimum_available), available_total)
    current = sum(
        normalize_fulltext_status(p.fulltext_status) == "available" for p in core_out
    )
    if current < target:
        eligible = sorted(
            (p for p in candidate_out
             if normalize_fulltext_status(p.fulltext_status) == "available"),
            key=lambda p: -(p.relevance_score or 0),
        )
        promoted = eligible[: target - current]
    else:
        promoted = []

    promoted_ids = {p.id for p in promoted}
    core_out.extend(promoted)
    candidate_out = [p for p in candidate_out if p.id not in promoted_ids]
    for p in core_out:
        p.layer = "core"
    for p in candidate_out:
        p.layer = "search"
    return core_out, candidate_out, [p.id for p in promoted]


def adaptive_tier(
    scored: list[Paper],
    min_core: int = _MIN_CORE,
    max_core: int = _MAX_CORE,
    max_candidates: int = _MAX_CANDIDATES,
    has_embeddings: bool = True,
) -> tuple[list[Paper], list[Paper]]:
    """Split reranked papers into (core, candidates) — no fixed X/Y counts.

    Candidate pool: score >= max(absolute floor, top1 * relative floor),
    capped at max_candidates. Core set: the head of the pool, cut at the
    sharpest relative score drop (elbow) within [min_core, max_core].
    """
    if not scored:
        return [], []

    top1 = scored[0].relevance_score
    tau = max(_ABS_TAU, top1 * _REL_TAU) if has_embeddings else top1 * 0.5
    pool = [p for p in scored if p.relevance_score >= tau]
    if len(pool) < min_core:
        pool = list(scored[:min(len(scored), min_core)])
    pool = pool[:max_candidates]

    k = min(len(pool), max_core)
    best_drop, best_i = 0.0, None
    # Only drops between two real consecutive papers count as an elbow; the
    # end of the pool is not a cut signal.
    for i in range(min_core, min(len(pool), max_core)):
        prev = pool[i - 1].relevance_score
        cur = pool[i].relevance_score
        if prev <= 0:
            continue
        drop = (prev - cur) / prev
        if drop > best_drop:
            best_drop, best_i = drop, i
    if best_i is not None and best_drop >= _ELBOW_DROP:
        k = max(min_core, best_i)

    core = pool[:k]
    candidates = pool[k:]
    for p in core:
        p.layer = "core"
    for p in candidates:
        p.layer = "search"
    return core, candidates


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def search_agent(state: ResearchState, progress_callback=None, invocation_context=None) -> ResearchState:
    """Run the full search pipeline. Outputs into state:
    papers (core set), candidates, sub_directions, search_queries, research_goal.
    """
    topic = (state.get("topic") or "").strip()
    conception = state.get("user_conception", "") or ""
    language = state.get("language", "both")

    def report(msg):
        if progress_callback:
            progress_callback(msg)

    # All stages share the search_papers tool budget (admin-configurable in
    # the tool-budget policy store) instead of each stage having its own
    # worst case: 30 s retrieval + 30 s probing + LLM used to overflow the
    # 45 s outer budget and void already-retrieved results.
    started = time.monotonic()
    try:
        from core.tool_budget_store import get_tool_budget
        total_budget = float(get_tool_budget("search_papers"))
    except Exception:  # pragma: no cover - degraded-storage fallback
        total_budget = 45.0
    deadline = (invocation_context.deadline if invocation_context is not None
                else started + total_budget)
    completed_stages: list[str] = []
    skipped_stages: list[str] = []
    initial_remaining = max(0.0, deadline - started)

    def remaining_budget() -> float:
        return deadline - time.monotonic()

    def publish_snapshot() -> None:
        state["completed_stages"] = list(completed_stages)
        state["skipped_stages"] = list(skipped_stages)
        callback = state.get("snapshot_callback")
        if callback:
            callback(state)

    # ① Understand
    report("理解研究意图、拆解研究方向...")
    try:
        understand_timeout = max(0.001, min(
            _UNDERSTAND_TIMEOUT_SECONDS, max(1.0, initial_remaining * 0.20)))
        plan = await asyncio.wait_for(
            _understand(topic, conception, language), timeout=understand_timeout)
    except TimeoutError:
        plan = _fallback_plan(topic)
        report("意图理解超时，已改用原始主题直接检索")
    completed_stages.append("intent")
    state["research_goal"] = plan["research_goal"]
    state["sub_directions"] = plan["sub_directions"]
    state["search_queries"] = plan["queries"]
    report(f"拆解为 {len(plan['sub_directions'])} 个方向、{len(plan['queries'])} 条检索式")

    # ② Multi-source retrieval + dedup
    s = get_settings()
    from core.paper_search_settings_store import get_paper_search_policy
    policy = get_paper_search_policy()
    storage_context = state.get("storage_context")
    enabled = [k for k, v in policy.sources.items() if v]
    try:
        manager = SearchManager(
            enabled_sources=enabled,
            results_per_source=s.search.results_per_source,
            search_deadline_seconds=min(
                float(policy.search_deadline_seconds),
                max(5.0, remaining_budget() - _SEARCH_TAIL_RESERVE_SECONDS)),
            per_source_timeout_seconds=policy.per_source_timeout_seconds,
            routing_mode=policy.routing_mode,
        )
    except TypeError:
        # Compatibility for injected test/extension managers that implement the
        # pre-runtime-policy constructor. The production manager accepts budgets.
        manager = SearchManager(
            enabled_sources=enabled, results_per_source=s.search.results_per_source)
    report(f"在 {len(enabled)} 个数据源中检索...")
    route_hints = {k: plan.get(k) for k in ("disciplines", "query_intents", "requested_sources", "requires_preprints", "requires_datasets")}
    try:
        all_papers = await manager.search_all(
            plan["queries"], progress_callback=report, route_hints=route_hints,
            topic=topic, deadline=deadline - _SEARCH_TAIL_RESERVE_SECONDS)
    except TypeError:
        # Compatibility for injected extension/test managers with the legacy signature.
        all_papers = await manager.search_all(plan["queries"], progress_callback=report)
    completed_stages.append("retrieval")
    state["search_route"] = getattr(manager, "last_route", {}) or {}
    used_sources = {
        outcome.source for outcome in (getattr(manager, "last_outcomes", []) or [])
        if getattr(outcome, "request_count", 0) or getattr(outcome, "papers", None)
    }
    source_notices: list[str] = []
    if "pubmed" in used_sources:
        source_notices.append(
            "PubMed/NLM 仅提供来源记录；记录收录不代表 NLM 对论文内容、结论或产品用途背书，请核对原始记录。"
        )
    state["source_notices"] = source_notices
    report(f"去重后共 {len(all_papers)} 篇")

    # ③ Semantic rerank.  Keep the paper list recoverable: when time is
    # short, use the deterministic no-embedding scorer rather than risking
    # that local model loading consumes the outer timeout.
    query_text = f"{plan['research_goal']}\n{topic}"
    if remaining_budget() >= 6.0:
        report("语义相关性重排（本地嵌入）...")
        try:
            rerank_timeout = max(0.1, remaining_budget() - 2.0)
            scored = await asyncio.wait_for(
                run_cpu_bound(rerank_papers, copy.deepcopy(all_papers), query_text),
                timeout=rerank_timeout,
            )
            has_embeddings = bool(scored) and scored[0].relevance_score >= 0 and _embed_ok(query_text)
            completed_stages.append("embedding_rerank")
        except (TimeoutError, asyncio.TimeoutError):
            report("本地嵌入重排超时，切换快速确定性重排...")
            scored = rerank_papers(all_papers, query_text, embed_fn=None)
            has_embeddings = False
            completed_stages.append("fast_rerank")
            skipped_stages.append("embedding_rerank")
            state["budget_exhausted"] = True
    else:
        report("剩余时间较少，使用快速确定性重排...")
        scored = rerank_papers(all_papers, query_text, embed_fn=None)
        has_embeddings = False
        completed_stages.append("fast_rerank")
        skipped_stages.append("embedding_rerank")
        state["budget_exhausted"] = True

    # ④ Adaptive tiering
    core, candidates = adaptive_tier(scored, has_embeddings=has_embeddings)
    completed_stages.append("tiering")
    report(f"分层完成：核心集 {len(core)} 篇，候选 {len(candidates)} 篇")

    # ④.5 Verified full-text availability. Search results and the genealogy
    # graph may show "全文可获取" only after a lightweight live-PDF probe;
    # a metadata pdf_url is never enough (it is often a paywalled landing
    # page). The actual PDF download happens later in deep_read.
    state["papers"] = core
    state["candidates"] = candidates
    publish_snapshot()
    fulltext_statuses: dict[str, str] = {}
    if (policy.verify_fulltext and policy.fulltext_verify_timeout_seconds > 0
            and policy.paper_fetch_mode != "disabled" and (core or candidates)):
        # The probe stage is a second network budget; clamp it to the tool
        # budget still remaining so it can never push the whole call past the
        # outer deadline after retrieval has already succeeded.
        probe_budget = min(float(policy.fulltext_verify_timeout_seconds),
                           remaining_budget() - _PROBE_TAIL_RESERVE_SECONDS)
        if probe_budget >= _PROBE_MIN_SECONDS:
            from tools.pdf.availability import verify_papers_fulltext

            report("探测各论文 OA 全文可获取性（只读取 PDF 文件头，不下载全文）...")
            try:
                fulltext_statuses = await verify_papers_fulltext(
                    core + candidates,
                    storage_context=storage_context,
                    progress_callback=report,
                    timeout_seconds=probe_budget,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("fulltext verification degraded to unknown: %s", e)
            completed_stages.append("fulltext_probe")
            n_avail = sum(v == "available" for v in fulltext_statuses.values())
            n_unavail = sum(v == "unavailable" for v in fulltext_statuses.values())
            n_unknown = len(core) + len(candidates) - n_avail - n_unavail
            report(f"全文探测完成：可获取 {n_avail} · 不可获取 {n_unavail} · 待验证 {n_unknown}")
        else:
            skipped_stages.append("fulltext_probe")
            state["budget_exhausted"] = True
            report("剩余时间不足，跳过全文探测（全文状态保持待验证）")
    state["fulltext_statuses"] = fulltext_statuses
    if not policy.verify_fulltext or policy.paper_fetch_mode == "disabled":
        skipped_stages.append("fulltext_probe")

    # ④.6 Best-effort readable core guarantee. Availability is authoritative
    # only after the probe above; doing this before verification would promote
    # paywalled landing pages merely because they looked like PDF URLs.
    core, candidates, promoted_ids = rebalance_core_for_fulltext(core, candidates)
    state["papers"] = core
    state["candidates"] = candidates
    core_available = sum(p.fulltext_status == "available" for p in core)
    total_available = sum(p.fulltext_status == "available" for p in core + candidates)
    target_available = min(_MIN_FULLTEXT_CORE, total_available)
    state["promoted_fulltext_core_ids"] = promoted_ids
    state["fulltext_core_available"] = core_available
    state["fulltext_core_target"] = target_available
    if promoted_ids:
        report(
            f"核心层全文保障：提升 {len(promoted_ids)} 篇全文可读候选，"
            f"核心层现有 {core_available} 篇全文可读论文"
        )
    elif core or candidates:
        report(
            f"核心层全文保障：核心层 {core_available} 篇全文可读论文"
            f"（本次可达目标 {target_available} 篇）"
        )

    # ⑤ Persist + index (best-effort). SQLite/Chroma and local embeddings are
    # synchronous, so keep them off the shared FastAPI event loop.
    publish_snapshot()
    session_id = state.get("session_id", "")
    if remaining_budget() >= 2.0:
        await run_cpu_bound(
            _persist_and_index_results, core + candidates, storage_context,
            s.storage.sqlite_path, session_id,
        )
        completed_stages.append("persistence")
    else:
        skipped_stages.append("persistence")
        state["budget_exhausted"] = True
        report("剩余时间不足，跳过 SQLite/向量索引增强")

    state["current_phase"] = "search_done"
    state["completed_stages"] = completed_stages
    state["skipped_stages"] = skipped_stages
    publish_snapshot()
    return state


def _persist_and_index_results(
    papers: list[Paper], storage_context, sqlite_path: str, session_id: str,
) -> None:
    """Synchronous search persistence, intended for ``run_cpu_bound``."""
    try:
        db = (
            Database(storage_context=storage_context)
            if storage_context is not None else Database(sqlite_path)
        )
        db.save_papers(papers)
        db.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("sqlite persist skipped: %s", e)

    if not session_id:
        return
    try:
        from tools.storage.vectorstore import VectorStore

        vs = VectorStore(storage_context=storage_context)
        for paper in papers:
            vs.upsert_paper_summary(paper, None, session_id=session_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "vector index skipped (ask_papers will self-heal on demand): %s", e
        )


def _embed_ok(query_text: str) -> bool:
    """Probe whether the embedder path works (rerank already tried it)."""
    try:
        from core.embeddings import get_embedder
        return get_embedder() is not None
    except Exception:  # noqa: BLE001
        return False
