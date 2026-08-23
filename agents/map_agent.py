"""Research map + reading path agent.

research_map: embedding clustering → 1 LLM call labels all clusters →
deterministic timeline → 1 LLM call landscape → genealogy graph data.
reading_path: deterministic picks (foundational / per-cluster / frontier) →
1 LLM call writes the per-paper reason.

LLM budget: research_map = 2 calls, reading_path = 1 call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from langchain_core.messages import HumanMessage

from core.embeddings import embed_texts
from core.blocking import run_cpu_bound
from core.llm import ainvoke_utility, get_llm
from core.models import Paper
from core.paper_search_settings_store import paper_abstract_text
from core.prompts.map_path import get_map_summary_prompt, get_reading_path_prompt
from tools.retrieval.bm25 import tokenize

logger = logging.getLogger(__name__)


def _lang_instruction(language: str) -> str:
    return {"zh": "用中文输出。", "en": "Respond in English."}.get(language, "用中文输出。")


def _parse_json_obj(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else parts[0]
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return data if isinstance(data, dict) else None
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def cluster_papers(papers: list[Paper], sub_directions: list | None = None,
                   embed_fn=embed_texts, vectors=None) -> dict[str, int]:
    """Assign each paper a cluster index.

    Embedding path: AgglomerativeClustering with a distance threshold, so the
    cluster count is adaptive (no user-facing Z). Fallback (no embedder /
    tiny set): best token-overlap match against sub_directions, else one
    cluster. Returns {paper_id: cluster_idx}.
    """
    if not papers:
        return {}
    if len(papers) <= 2:
        return {p.id: 0 for p in papers}

    if vectors is not None or embed_fn is not None:
        try:
            vecs = vectors
            if vecs is None:
                docs = [f"{p.title or ''}\n{paper_abstract_text(p)[:300]}" for p in papers]
                vecs = embed_fn(docs)
            if vecs is not None and len(vecs) == len(papers):
                from sklearn.cluster import AgglomerativeClustering
                import numpy as np

                x = np.array(vecs)
                labels = None
                for threshold in (0.5, 0.4):
                    model = AgglomerativeClustering(
                        n_clusters=None, distance_threshold=threshold,
                        metric="cosine", linkage="average",
                    )
                    labels = model.fit_predict(x)
                    # A single blob over many papers means the threshold was
                    # too coarse — retry tighter once.
                    if len(set(labels.tolist())) > 1 or len(papers) <= 6:
                        break
                out: dict[str, int] = {}
                remap: dict[int, int] = {}
                for p, lab in zip(papers, labels):
                    idx = remap.setdefault(int(lab), len(remap))
                    out[p.id] = idx
                return out
        except Exception as e:  # noqa: BLE001
            logger.warning("embedding clustering failed, falling back: %s", e)

    # Fallback: group by best-matching sub_direction (token overlap).
    directions = [d.get("name", "") for d in (sub_directions or []) if isinstance(d, dict)]
    if not directions:
        return {p.id: 0 for p in papers}
    dir_tokens = [set(tokenize(name)) for name in directions]
    out = {}
    for p in papers:
        ptoks = set(tokenize(p.title or ""))
        best, best_score = 0, -1
        for i, dtoks in enumerate(dir_tokens):
            score = len(ptoks & dtoks)
            if score > best_score:
                best, best_score = i, score
        out[p.id] = best
    return out


# ---------------------------------------------------------------------------
# Research map
# ---------------------------------------------------------------------------

def build_timeline(papers: list[Paper]) -> list[dict]:
    """Deterministic year-grouped timeline (no LLM)."""
    by_year: dict[int, list[Paper]] = {}
    for p in papers:
        if p.year:
            by_year.setdefault(p.year, []).append(p)
    out = []
    for year in sorted(by_year):
        group = sorted(by_year[year], key=lambda p: p.citation_count or 0, reverse=True)
        out.append({
            "year": year,
            "papers": [{"id": p.id, "title": p.title,
                        "citation_count": p.citation_count or 0} for p in group],
        })
    return out


def _fallback_landscape(clusters: list[dict], papers_by_id: dict[str, Paper], language: str) -> str:
    if not clusters:
        return ""
    parts = []
    for cluster in clusters:
        titles = [papers_by_id[pid].title for pid in cluster.get("paper_ids", [])[:2]
                  if pid in papers_by_id]
        parts.append(f"{cluster['label']}（{'；'.join(titles)}）" if titles else cluster["label"])
    years = sorted(p.year for p in papers_by_id.values() if p.year)
    period = f"{years[0]}–{years[-1]}" if years else "当前"
    if language == "en":
        return f"Across {period}, the collection develops through " + "; ".join(parts) + "."
    return f"该论文集覆盖 {period} 年，主要形成" + "、".join(parts) + "等方向；建议从高被引代表作进入，再沿时间线观察方向分化与交叉。"


async def _summarize_map(clusters: list[dict], papers_by_id: dict[str, Paper],
                         language: str) -> tuple[str, str]:
    """One bounded utility call produces cluster labels and the landscape."""
    clusters_text = "\n".join(
        f"簇 {c['id']}:\n" + "\n".join(
            f"  - {papers_by_id[pid].title}" for pid in c["paper_ids"] if pid in papers_by_id)
        for c in clusters
    )
    prompt = get_map_summary_prompt().format(
        clusters_text=clusters_text, language_instruction=_lang_instruction(language))
    try:
        llm = get_llm("light")
        async with asyncio.timeout(15.0):
            resp = await ainvoke_utility(llm, [HumanMessage(content=prompt)])
        data = _parse_json_obj(resp.content if hasattr(resp, "content") else str(resp))
        if not data:
            raise ValueError("map summary response was not JSON")
        items = [item for item in data.get("clusters", []) if isinstance(item, dict)]
        by_id = {}
        for item in items:
            digits = re.sub(r"\D", "", str(item.get("id", "")))
            if digits:
                by_id[int(digits)] = item
        for pos, cluster in enumerate(clusters):
            item = by_id.get(cluster["id"]) or (items[pos] if pos < len(items) else None)
            if item:
                cluster["label"] = str(item.get("label") or cluster["label"])[:60]
                cluster["overview"] = str(item.get("overview") or "")[:500]
        landscape = str(data.get("landscape") or "").strip()
        if not landscape:
            landscape = _fallback_landscape(clusters, papers_by_id, language)
        return landscape, "available"
    except Exception as exc:  # noqa: BLE001
        logger.warning("map summary failed, using deterministic fallback: %s", exc)
        return _fallback_landscape(clusters, papers_by_id, language), "fallback"


def _fallback_graph(papers: list[Paper], cluster_of: dict[str, int], status: str) -> dict:
    from tools.storage.genealogy import _best_url
    nodes = [{
        "id": p.id, "title": p.title, "year": p.year or 0,
        "citation_count": p.citation_count or 0, "cluster": cluster_of.get(p.id, 0),
        "role": "", "layer": "core" if p.layer == "core" else "candidate",
        "authors": (p.authors or [])[:3], "url": _best_url(p),
        "abstract": paper_abstract_text(p)[:200], "venue": p.venue or "",
    } for p in papers]
    return {"nodes": nodes, "edges": [], "citation_enrichment_status": status}


async def build_research_map(papers: list[Paper], candidates: list[Paper],
                             sub_directions: list, language: str,
                             progress=None, citation_mode: str = "fast") -> dict:
    """Fast research map with bounded, independently degrading enhancements."""
    from tools.storage.genealogy import build_genealogy

    started = time.monotonic()
    stage_ms: dict[str, float] = {}
    degraded_reasons: list[str] = []
    all_papers = list(papers) + list(candidates)
    papers_by_id = {p.id: p for p in all_papers}

    def report(message: str) -> None:
        if progress:
            progress(message)

    vectors = None
    embedding_status = "skipped"
    embed_started = time.monotonic()
    if len(all_papers) >= 2:
        try:
            report("计算一次语义向量，用于聚类和关系边...")
            docs = [f"{p.title or ''}\n{paper_abstract_text(p)[:300]}" for p in all_papers]
            async with asyncio.timeout(12.0):
                vectors = await run_cpu_bound(embed_texts, docs)
            if not vectors or len(vectors) != len(all_papers):
                raise ValueError("embedding count mismatch")
            embedding_status = "available"
        except TimeoutError:
            embedding_status = "timeout"
            degraded_reasons.append("embedding_timeout")
            vectors = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("map embedding failed: %s", exc)
            embedding_status = "fallback"
            degraded_reasons.append("embedding_unavailable")
            vectors = None
    stage_ms["embedding"] = round((time.monotonic() - embed_started) * 1000, 1)

    cluster_started = time.monotonic()
    cluster_of = await run_cpu_bound(
        cluster_papers, all_papers, sub_directions, None, vectors)
    groups: dict[int, list[str]] = {}
    for pid, cid in cluster_of.items():
        groups.setdefault(cid, []).append(pid)
    clusters = []
    for cid in sorted(groups):
        pids = sorted(groups[cid], key=lambda pid: papers_by_id[pid].citation_count or 0,
                      reverse=True)
        clusters.append({
            "id": cid, "label": f"主题簇 {cid + 1}", "overview": "",
            "paper_ids": pids,
            "papers": [{"id": pid, "title": papers_by_id[pid].title,
                        "year": papers_by_id[pid].year,
                        "citation_count": papers_by_id[pid].citation_count or 0}
                       for pid in pids],
        })
    timeline = build_timeline(all_papers)
    stage_ms["clustering"] = round((time.monotonic() - cluster_started) * 1000, 1)

    report("并行生成簇概述与批量引文增强...")
    summary_started = time.monotonic()
    graph_started = summary_started
    summary_task = asyncio.create_task(_summarize_map(clusters, papers_by_id, language))
    graph_task = asyncio.create_task(build_genealogy(
        all_papers, cluster_of, embed_fn=None, vectors=vectors, citation_mode=citation_mode))
    landscape, summary_status = await summary_task
    stage_ms["summary"] = round((time.monotonic() - summary_started) * 1000, 1)
    try:
        graph = await graph_task
    except Exception as exc:  # noqa: BLE001
        logger.warning("genealogy enhancement failed, returning node-only map: %s", exc)
        graph = _fallback_graph(all_papers, cluster_of, "unavailable")
        degraded_reasons.append("genealogy_unavailable")
    stage_ms["genealogy"] = round((time.monotonic() - graph_started) * 1000, 1)
    if summary_status != "available":
        degraded_reasons.append("summary_fallback")
    citation_status = graph.get("citation_enrichment_status", "unavailable")
    if citation_status not in {
        "available", "no_doi", "disabled", "source_capability_disabled",
    }:
        degraded_reasons.append(f"citation_{citation_status}")
    stage_ms["total"] = round((time.monotonic() - started) * 1000, 1)
    degraded = bool(degraded_reasons)
    logger.info(
        "research_map_complete papers=%d citation_mode=%s citation_status=%s "
        "embedding_status=%s summary_status=%s degraded=%s stage_ms=%s reasons=%s",
        len(all_papers), citation_mode, citation_status, embedding_status,
        summary_status, degraded, stage_ms, ",".join(degraded_reasons))
    report("研究地图已生成")
    return {
        "clusters": clusters, "timeline": timeline, "landscape": landscape,
        "graph": graph, "degraded": degraded,
        "embedding_status": embedding_status, "summary_status": summary_status,
        "citation_mode": citation_mode,
        "citation_enrichment_status": citation_status,
        "stage_ms": stage_ms, "degraded_reasons": degraded_reasons,
    }


# ---------------------------------------------------------------------------
# Reading path
# ---------------------------------------------------------------------------

def pick_reading_order(papers: list[Paper], cluster_of: dict[str, int],
                       roles: dict[str, str], max_picks: int = 6) -> list[dict]:
    """Deterministic reading-order picks (pure).

    Order: 奠基 (role from centrality) → 各簇代表 (per-cluster top-cited,
    labelled 桥梁) → 前沿 (recent high-cited). Deduped, <= max_picks.
    """
    picks: list[dict] = []
    seen: set[str] = set()

    def add(p: Paper, role: str):
        if p.id in seen or len(picks) >= max_picks:
            return
        seen.add(p.id)
        picks.append({"paper_id": p.id, "title": p.title, "role": role})

    by_id = {p.id: p for p in papers}
    for pid, role in roles.items():
        if role == "foundational" and pid in by_id:
            add(by_id[pid], "奠基")

    groups: dict[int, list[Paper]] = {}
    for p in papers:
        groups.setdefault(cluster_of.get(p.id, 0), []).append(p)
    for cid in sorted(groups):
        top = sorted(groups[cid], key=lambda p: p.citation_count or 0, reverse=True)
        if top:
            add(top[0], "桥梁")

    years = [p.year for p in papers if p.year]
    if years:
        frontier_cut = max(years) - 1
        frontier = sorted(
            [p for p in papers if p.year and p.year >= frontier_cut],
            key=lambda p: p.citation_count or 0, reverse=True,
        )
        for p in frontier:
            add(p, "前沿")
    return picks


async def build_reading_path(papers: list[Paper], map_data: dict,
                             language: str, progress=None) -> list[dict]:
    """Reading path: deterministic picks + 1 LLM call for reasons."""
    cluster_of: dict[str, int] = {}
    roles: dict[str, str] = {}
    for c in (map_data or {}).get("clusters", []):
        for pid in c.get("paper_ids", []):
            cluster_of[pid] = int(c.get("id", 0))
    for node in (map_data or {}).get("graph", {}).get("nodes", []):
        if node.get("role"):
            roles[node["id"]] = node["role"]

    picks = pick_reading_order(papers, cluster_of, roles)
    if not picks:
        return picks

    papers_text = "\n".join(
        f"- [{pk['paper_id']}] ({pk['role']}) {pk['title']}" for pk in picks
    )
    prompt = get_reading_path_prompt().format(
        papers_text=papers_text,
        language_instruction=_lang_instruction(language),
    )
    reasons: dict[str, str] = {}
    try:
        llm = get_llm("light")
        resp = await ainvoke_utility(llm, [HumanMessage(content=prompt)])
        data = _parse_json_obj(resp.content if hasattr(resp, "content") else str(resp))
        for r in (data or {}).get("reasons", []):
            if isinstance(r, dict) and r.get("paper_id"):
                reasons[str(r["paper_id"])] = str(r.get("reason", ""))[:200]
    except Exception as e:  # noqa: BLE001
        logger.warning("reading-path reasons failed: %s", e)

    for pk in picks:
        pk["reason"] = reasons.get(pk["paper_id"], "")
    return picks
