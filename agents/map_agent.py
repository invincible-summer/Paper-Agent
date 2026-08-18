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

from langchain_core.messages import HumanMessage

from core.embeddings import embed_texts
from core.blocking import run_cpu_bound
from core.llm import ainvoke_utility, get_llm
from core.models import Paper
from core.prompts.map_path import (
    get_cluster_labels_prompt,
    get_landscape_prompt,
    get_reading_path_prompt,
)
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
                   embed_fn=embed_texts) -> dict[str, int]:
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

    if embed_fn is not None:
        try:
            docs = [f"{p.title or ''}\n{(p.abstract or '')[:300]}" for p in papers]
            vecs = embed_fn(docs)
            if vecs and len(vecs) == len(papers):
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


async def _label_clusters(clusters: list[dict], papers_by_id: dict[str, Paper],
                          language: str) -> None:
    """One LLM call labels all clusters in place (label + overview)."""
    clusters_text = "\n".join(
        f"簇 {c['id']}:\n" + "\n".join(
            f"  - {papers_by_id[pid].title}" for pid in c["paper_ids"] if pid in papers_by_id
        )
        for c in clusters
    )
    prompt = get_cluster_labels_prompt().format(
        clusters_text=clusters_text,
        language_instruction=_lang_instruction(language),
    )
    try:
        llm = get_llm("light")
        async with asyncio.timeout(12.0):
            resp = await ainvoke_utility(llm, [HumanMessage(content=prompt)])
        data = _parse_json_obj(resp.content if hasattr(resp, "content") else str(resp))
    except Exception as e:  # noqa: BLE001
        logger.warning("cluster labeling failed: %s", e)
        data = None
    items = [c for c in (data or {}).get("clusters", []) if isinstance(c, dict)]
    # The model often echoes the id as "簇0" instead of "0" — normalize to
    # digits, with a positional fallback when ids can't be matched at all.
    by_id: dict[int, dict] = {}
    for item in items:
        digits = re.sub(r"\D", "", str(item.get("id", "")))
        if digits:
            by_id[int(digits)] = item
    for pos, c in enumerate(clusters):
        hit = by_id.get(c["id"])
        if hit is None and pos < len(items):
            hit = items[pos]
        if hit:
            c["label"] = str(hit.get("label") or c["label"])[:60]
            c["overview"] = str(hit.get("overview") or "")[:500]


async def _landscape(clusters: list[dict], papers_by_id: dict[str, Paper],
                     language: str) -> str:
    """One LLM call: the field's overall landscape paragraph.

    Cluster labels/overviews are the primary material; when labeling failed
    (empty overviews) the top paper titles keep the call grounded instead of
    letting the model refuse for lack of content.
    """
    lines = []
    for c in clusters:
        line = f"- {c['label']}: {c.get('overview', '')}"
        if not c.get("overview"):
            titles = [papers_by_id[pid].title for pid in c.get("paper_ids", [])[:4]
                      if pid in papers_by_id]
            if titles:
                line += "（代表论文：" + "；".join(titles) + "）"
        lines.append(line)
    clusters_text = "\n".join(lines)
    prompt = get_landscape_prompt().format(
        clusters_text=clusters_text,
        language_instruction=_lang_instruction(language),
    )
    try:
        llm = get_llm("light")
        async with asyncio.timeout(12.0):
            resp = await ainvoke_utility(llm, [HumanMessage(content=prompt)])
        return (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("landscape generation failed: %s", e)
        return ""


async def build_research_map(papers: list[Paper], candidates: list[Paper],
                             sub_directions: list, language: str,
                             progress=None) -> dict:
    """Full research map: clusters + timeline + landscape + genealogy graph."""
    def report(msg):
        if progress:
            progress(msg)

    from tools.storage.genealogy import build_genealogy

    all_papers = list(papers) + list(candidates)
    report("语义聚类主题簇...")
    cluster_of = await run_cpu_bound(cluster_papers, all_papers, sub_directions)

    papers_by_id = {p.id: p for p in all_papers}
    groups: dict[int, list[str]] = {}
    for pid, cid in cluster_of.items():
        groups.setdefault(cid, []).append(pid)

    clusters = []
    for cid in sorted(groups):
        pids = sorted(groups[cid],
                      key=lambda pid: papers_by_id[pid].citation_count or 0,
                      reverse=True)
        clusters.append({
            "id": cid,
            "label": f"主题簇 {cid + 1}",
            "overview": "",
            "paper_ids": pids,
            "papers": [{"id": pid, "title": papers_by_id[pid].title,
                        "year": papers_by_id[pid].year,
                        "citation_count": papers_by_id[pid].citation_count or 0}
                       for pid in pids],
        })

    report("生成簇标签与领域脉络...")
    await _label_clusters(clusters, papers_by_id, language)
    landscape = await _landscape(clusters, papers_by_id, language)
    timeline = build_timeline(all_papers)

    report("构建论文谱系图数据...")
    graph = await build_genealogy(all_papers, cluster_of)
    report("论文谱系图数据已生成")

    return {
        "clusters": clusters,
        "timeline": timeline,
        "landscape": landscape,
        "graph": graph,
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
