"""Genealogy graph data builder (论文谱系图).

Produces the JSON the frontend SVG component renders — NO pyvis/HTML:

  nodes: [{id, title, year, citation_count, cluster, role, layer,
           authors, url, abstract, venue, fulltext, fulltext_status}]
  edges: [{source, target, type}]   type = "cites" (real, directed) | "semantic"

Edge policy:
  1. Real citation edges come from OpenAlex referenced_works
     (tools/search/openalex_refs.py, free, no key). Direction: citer -> cited.
  2. Semantic edges fill the gaps: local-embedding cosine >= _SEMANTIC_TAU,
     skipping pairs already connected by a citation edge. When the embedder
     is unavailable the graph degrades to citation-only (or empty) — the
     caller still gets clusters from its own fallback.

Centrality (networkx, citation subgraph): PageRank marks 奠基 (foundational)
nodes, betweenness marks 桥梁 (bridge) nodes; recent high-cited nodes are
前沿 (frontier). Roles feed both the graph node styling and reading_path.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from core.embeddings import embed_texts
from core.models import Paper
from core.paper_search_settings_store import paper_abstract_text
from core.reading_policy import (
    fulltext_available,
    normalize_fulltext_status,
)

logger = logging.getLogger(__name__)
_CITATION_ENRICHMENT_TIMEOUT_SECONDS = 8.0
_CITATION_TIMEOUTS = {"fast": 3.0, "quality": 8.0, "off": 0.0}

_SEMANTIC_TAU = 0.5        # cosine threshold for semantic edges
_MAX_SEMANTIC_EDGES_PER_NODE = 3


def compute_semantic_edges(papers: list[Paper], existing_pairs: set[tuple[str, str]],
                           embed_fn=embed_texts, vectors=None) -> list[dict]:
    """Cosine-similarity edges between papers not already citation-linked.

    Pure-ish (embed_fn injectable). Returns [{source, target, type:"semantic",
    weight}]. Caps edges per node to avoid a hairball.
    """
    if (embed_fn is None and vectors is None) or len(papers) < 2:
        return []
    try:
        vecs = vectors
        if vecs is None:
            docs = [f"{p.title or ''}\n{paper_abstract_text(p)[:300]}" for p in papers]
            vecs = embed_fn(docs)
    except Exception as e:  # noqa: BLE001
        logger.warning("semantic edge embedding failed: %s", e)
        return []
    if not vecs or len(vecs) != len(papers):
        return []

    def cos(a, b) -> float:
        return sum(x * y for x, y in zip(a, b))

    edges: list[dict] = []
    degree: dict[str, int] = {}
    for i in range(len(papers)):
        for j in range(i + 1, len(papers)):
            pi, pj = papers[i], papers[j]
            if (pi.id, pj.id) in existing_pairs or (pj.id, pi.id) in existing_pairs:
                continue
            sim = cos(vecs[i], vecs[j])
            if sim < _SEMANTIC_TAU:
                continue
            if degree.get(pi.id, 0) >= _MAX_SEMANTIC_EDGES_PER_NODE:
                continue
            if degree.get(pj.id, 0) >= _MAX_SEMANTIC_EDGES_PER_NODE:
                continue
            edges.append({"source": pi.id, "target": pj.id,
                          "type": "semantic", "weight": round(sim, 3)})
            degree[pi.id] = degree.get(pi.id, 0) + 1
            degree[pj.id] = degree.get(pj.id, 0) + 1
    return edges


def compute_roles(papers: list[Paper], citation_edges: list[tuple[str, str]]) -> dict[str, str]:
    """Assign notable-node roles: 奠基 (PageRank top) / 桥梁 (betweenness top).

    Frontier nodes are picked by the reading-path logic instead (they depend
    on recency, not centrality). Returns {paper_id: role}.
    """
    roles: dict[str, str] = {}
    if not papers:
        return roles
    try:
        import networkx as nx
    except ImportError:
        return roles

    g = nx.DiGraph()
    g.add_nodes_from(p.id for p in papers)
    g.add_edges_from(citation_edges)

    if citation_edges:
        try:
            pr = nx.pagerank(g)
            top_pr = sorted(pr, key=pr.get, reverse=True)[:2]
            for pid in top_pr:
                roles[pid] = "foundational"
        except Exception:  # noqa: BLE001
            pass
        try:
            bc = nx.betweenness_centrality(g)
            for pid in sorted(bc, key=bc.get, reverse=True):
                if bc[pid] > 0 and pid not in roles:
                    roles[pid] = "bridge"
                    break
        except Exception:  # noqa: BLE001
            pass
    else:
        # No citation facts: fall back to raw citation counts for foundational.
        top = sorted(papers, key=lambda p: p.citation_count or 0, reverse=True)[:2]
        for p in top:
            if (p.citation_count or 0) > 0:
                roles[p.id] = "foundational"
    return roles


async def fetch_citation_edges(papers: list[Paper], citation_mode: str = "fast") -> tuple[list[tuple[str, str]], str]:
    """Fetch in-library citation edges using one batched OpenAlex request."""
    from tools.search import openalex_refs
    if citation_mode == "off":
        return [], "disabled"
    dois = [p.doi for p in papers if p.doi]
    if not dois:
        return [], "no_doi"
    try:
        from core.paper_search_settings_store import source_capability_enabled
        allowed, _reason = source_capability_enabled("openalex", "search")
        if not allowed:
            return [], "source_capability_disabled"
        timeout = _CITATION_TIMEOUTS.get(citation_mode, 3.0)
        async with asyncio.timeout(timeout):
            refs_by_doi, doi_to_oa, status = await openalex_refs.fetch_citation_bundle(dois)
        if status == "available":
            edges = openalex_refs.build_citation_edges(papers, refs_by_doi, doi_to_oa)
            return edges, status
        return [], status
    except TimeoutError:
        return [], "timeout"
    except Exception as exc:  # noqa: BLE001
        logger.warning("citation edge fetch failed: %s", exc)
        return [], "unavailable"


async def build_genealogy(papers: list[Paper], cluster_of: dict[str, int],
                          embed_fn=embed_texts, *, vectors=None, citation_mode: str = "fast") -> dict[str, Any]:
    """Full genealogy payload for the frontend.

    papers: core set + candidates. cluster_of: paper_id -> cluster index.
    Returns {nodes, edges}.
    """
    try:
        configured_timeout = min(
            _CITATION_TIMEOUTS.get(citation_mode, _CITATION_ENRICHMENT_TIMEOUT_SECONDS),
            _CITATION_ENRICHMENT_TIMEOUT_SECONDS,
        )
        async with asyncio.timeout(configured_timeout or 0.001):
            try:
                citation_result = await fetch_citation_edges(papers, citation_mode)
            except TypeError as exc:
                # Compatibility for tests/plugins that monkeypatch the legacy
                # one-argument helper. Do not mask unrelated TypeErrors.
                if "positional argument" not in str(exc):
                    raise
                citation_result = await fetch_citation_edges(papers)
            if isinstance(citation_result, tuple) and len(citation_result) == 2:
                citation_edges, citation_status = citation_result
            else:  # compatibility for tests/third-party monkeypatches
                citation_edges = list(citation_result or [])
                citation_status = "available" if citation_edges else "unavailable"
    except TimeoutError:
        citation_edges, citation_status = [], "timeout"
    cite_pairs = set(citation_edges)
    from core.blocking import run_cpu_bound
    semantic_edges, roles = await run_cpu_bound(
        _compute_graph_enrichment, papers, cite_pairs, citation_edges, embed_fn, vectors
    )

    nodes = []
    for p in papers:
        status = normalize_fulltext_status(getattr(p, "fulltext_status", ""))
        nodes.append({
            "id": p.id,
            "title": p.title,
            "year": p.year or 0,
            "citation_count": p.citation_count or 0,
            "cluster": cluster_of.get(p.id, 0),
            "role": roles.get(p.id, ""),
            "layer": "core" if p.layer == "core" else "candidate",
            "authors": (p.authors or [])[:3],
            "url": _best_url(p),
            "abstract": paper_abstract_text(p)[:200],
            "venue": p.venue or "",
            # Only a verified live-PDF probe (or a real full_text summary)
            # may show as full-text available. A metadata pdf_url is often a
            # paywalled landing page and must never light up this badge.
            "fulltext": fulltext_available(p),
            "fulltext_status": status,
        })
    edges = (
        [{"source": s, "target": t, "type": "cites", "weight": 1.0}
         for s, t in citation_edges]
        + semantic_edges
    )
    return {
        "nodes": nodes,
        "edges": edges,
        "citation_enrichment_status": citation_status,
    }


def _compute_graph_enrichment(papers, cite_pairs, citation_edges, embed_fn, vectors=None):
    """CPU-only graph work, kept outside the shared asyncio event loop."""
    semantic_edges = compute_semantic_edges(papers, cite_pairs, embed_fn=embed_fn, vectors=vectors)
    roles = compute_roles(papers, citation_edges)
    return semantic_edges, roles


def _best_url(p: Paper) -> str:
    if p.urls:
        for src in ("doi", "openalex", "arxiv", "crossref", "europepmc", "doaj"):
            u = p.urls.get(src)
            if u and u.startswith(("http://", "https://")):
                return u
    if p.doi:
        return f"https://doi.org/{p.doi.lstrip('/')}"
    return p.pdf_url or ""
