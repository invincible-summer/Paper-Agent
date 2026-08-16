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

import logging
import math
from typing import Any

from core.embeddings import embed_texts
from core.models import Paper
from core.reading_policy import (
    fulltext_available,
    normalize_fulltext_status,
)

logger = logging.getLogger(__name__)

_SEMANTIC_TAU = 0.5        # cosine threshold for semantic edges
_MAX_SEMANTIC_EDGES_PER_NODE = 3


def compute_semantic_edges(papers: list[Paper], existing_pairs: set[tuple[str, str]],
                           embed_fn=embed_texts) -> list[dict]:
    """Cosine-similarity edges between papers not already citation-linked.

    Pure-ish (embed_fn injectable). Returns [{source, target, type:"semantic",
    weight}]. Caps edges per node to avoid a hairball.
    """
    if embed_fn is None or len(papers) < 2:
        return []
    try:
        docs = [f"{p.title or ''}\n{(p.abstract or '')[:300]}" for p in papers]
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


async def fetch_citation_edges(papers: list[Paper]) -> list[tuple[str, str]]:
    """Real directed citation edges among the given papers (OpenAlex).

    Any failure degrades to [] — the graph still renders with semantic edges.
    """
    from tools.search import openalex_refs

    dois = [p.doi for p in papers if p.doi]
    if not dois:
        return []
    try:
        refs_by_doi = await openalex_refs.fetch_referenced_works(dois)
        doi_to_oa = await openalex_refs.resolve_openalex_ids(dois)
        return openalex_refs.build_citation_edges(papers, refs_by_doi, doi_to_oa)
    except Exception as e:  # noqa: BLE001
        logger.warning("citation edge fetch failed: %s", e)
        return []


async def build_genealogy(papers: list[Paper], cluster_of: dict[str, int],
                          embed_fn=embed_texts) -> dict[str, Any]:
    """Full genealogy payload for the frontend.

    papers: core set + candidates. cluster_of: paper_id -> cluster index.
    Returns {nodes, edges}.
    """
    citation_edges = await fetch_citation_edges(papers)
    cite_pairs = set(citation_edges)
    semantic_edges = compute_semantic_edges(papers, cite_pairs, embed_fn=embed_fn)
    roles = compute_roles(papers, citation_edges)

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
            "abstract": (p.abstract or "")[:200],
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
    return {"nodes": nodes, "edges": edges}


def _best_url(p: Paper) -> str:
    if p.urls:
        for src in ("doi", "openalex", "arxiv", "crossref", "europepmc", "doaj"):
            u = p.urls.get(src)
            if u and u.startswith(("http://", "https://")):
                return u
    if p.doi:
        return f"https://doi.org/{p.doi.lstrip('/')}"
    return p.pdf_url or ""
