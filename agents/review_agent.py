"""Review Synthesizer Agent: literature review writing (DESIGN D-015).

Pipeline:
  1. Organize by topic clusters (from Graph Builder)
  2. Write introduction
  3. Write each cluster section (with anti-hallucination)
  4. Write cross-topic analysis
  5. Write conclusion
  6. Self-review + rewrite (1 or 2 rounds, user selectable)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from langchain_core.messages import HumanMessage

from core.config import get_settings
from core.llm import get_llm
from core.models import Paper, PaperSummary
from core.paper_search_settings_store import paper_abstract_text
from core.prompts.review_prompts import (
    SECTION_WRITING_PROMPT,
    INTRODUCTION_PROMPT,
    CROSS_TOPIC_PROMPT,
    CONCLUSION_PROMPT,
    SELF_REVIEW_PROMPT,
    REWRITE_PROMPT,
    REVIEW_AND_REVISE_PROMPT,
)
from core.state import ResearchState

logger = logging.getLogger(__name__)


async def review_agent(state: ResearchState, progress_callback=None) -> ResearchState:
    """Generate a literature review from clustered papers."""
    def report(msg):
        if progress_callback:
            progress_callback(msg)

    topic = state.get("topic", "")
    conception = state.get("user_conception", "")
    language = state.get("language", "both")
    papers = state.get("papers", [])
    summaries = state.get("paper_summaries", {})
    graph_data = state.get("citation_graph_data", {})
    review_rounds = state.get("review_rounds", 0)

    lang_suffix = {
        "zh": "\n\n请全程使用中文撰写。",
        "en": "\n\nWrite entirely in English.",
    }.get(language, "\n\n请全程使用中文撰写。")

    if not papers:
        report("No papers available for review")
        state["current_phase"] = "review_done"
        return state

    # Build cluster info
    clusters = graph_data.get("clusters", {})
    if not clusters:
        # Fallback: single cluster with all papers
        clusters = {"0": {"label": "All Papers", "paper_ids": [p.id for p in papers]}}

    # Sort clusters by size (descending), take top Z for detailed sections (DESIGN D-034)
    review_cluster_count = state.get("review_cluster_count", 3)
    sorted_clusters = sorted(
        clusters.items(),
        key=lambda item: len(item[1].get("paper_ids", [])),
        reverse=True,
    )
    top_clusters = sorted_clusters[:review_cluster_count]
    remaining_clusters = sorted_clusters[review_cluster_count:]

    report(
        f"Writing literature review: {len(top_clusters)} of {len(clusters)} clusters "
        f"in detail, {review_rounds} round(s)..."
    )

    # Build paper lookup
    paper_map = {p.id: p for p in papers}
    valid_ids = set(paper_map.keys())

    # Build papers list text for prompts
    all_papers_text = _build_papers_list(papers, summaries)
    core_ids = set(p.id for p in papers)

    # --- Write sections ---
    sections = []

    # Introduction
    report("Writing introduction...")
    intro = await _write_section(
        INTRODUCTION_PROMPT,
        lang_suffix=lang_suffix,
        topic=topic,
        conception=conception or "(none provided)",
        papers_list=all_papers_text,
    )
    intro = _validate_citations(intro, valid_ids)
    sections.append(("Introduction", intro))

    # Each cluster
    for cid, cluster_info in top_clusters:
        label = cluster_info.get("label", f"Cluster {cid}")
        paper_ids = cluster_info.get("paper_ids", [])

        # Filter to papers we have summaries for
        cluster_papers = [paper_map[pid] for pid in paper_ids if pid in paper_map]
        if not cluster_papers:
            continue

        report(f"Writing section: {label} ({len(cluster_papers)} papers)...")

        papers_list_text = _build_papers_list(cluster_papers, summaries)
        section_text = await _write_section(
            SECTION_WRITING_PROMPT,
        lang_suffix=lang_suffix,
            topic=topic,
            cluster_label=label,
            papers_list=papers_list_text,
        )
        section_text = _validate_citations(section_text, valid_ids)

        # Review-and-revise rounds (merged: 1 LLM call instead of 2)
        for r in range(review_rounds):
            report(f"Review & revise round {r+1}/{review_rounds} for: {label}...")
            section_text = await _review_and_revise(section_text, topic, valid_ids)
            section_text = _validate_citations(section_text, valid_ids)

        sections.append((label, section_text))

    # Brief mention of remaining clusters (no LLM section generation)
    if remaining_clusters:
        remaining_labels = [
            cluster_info.get("label", f"Cluster {cid}")
            for cid, cluster_info in remaining_clusters
        ]
        remaining_text = (
            "Other relevant topics identified in the literature include: "
            + ", ".join(remaining_labels)
            + ". These clusters were not discussed in detail due to scope constraints."
        )
        sections.append(("Other Topics", remaining_text))

    # Cross-topic
    topics_summary = "\n".join(f"- {label}" for label, _ in sections[1:] if label != "Other Topics")
    report("Writing cross-topic analysis...")
    cross_papers = papers[:10]  # top 10 for cross-reference
    cross_text = await _write_section(
        CROSS_TOPIC_PROMPT,
        lang_suffix=lang_suffix,
        topic=topic,
        topics_summary=topics_summary,
        papers_list=_build_papers_list(cross_papers, summaries),
    )
    cross_text = _validate_citations(cross_text, valid_ids)
    sections.append(("Cross-Topic Analysis", cross_text))

    # Conclusion
    report("Writing conclusion...")
    gaps = _identify_gaps(papers, summaries, topic)
    conclusion_text = await _write_section(
        CONCLUSION_PROMPT,
        lang_suffix=lang_suffix,
        topic=topic,
        topics_summary=topics_summary,
        gaps=gaps,
        conception=conception or "(none provided)",
    )
    conclusion_text = _validate_citations(conclusion_text, valid_ids)
    sections.append(("Conclusion", conclusion_text))

    # Assemble full review
    full_review = "\n\n".join(f"## {label}\n\n{text}" for label, text in sections)

    report(f"Literature review complete ({len(sections)} sections)")

    # D-091: render [paper_id] citations as human-readable
    # "Title ([Link](url))" so the review shows paper titles + a link to the
    # original instead of opaque internal IDs. Done AFTER anti-hallucination
    # validation so the LLM still keys on stable ids; both structured and chat
    # paths flow through here.
    full_review = _render_citations_as_titles(full_review, paper_map)

    state["literature_review"] = full_review
    state["current_phase"] = "review_done"
    return state


def _build_papers_list(papers: list[Paper], summaries: dict[str, PaperSummary]) -> str:
    """Build a detailed text list of papers for prompt injection.

    Uses structured summaries when available (from Deep Read) for richer context.
    Falls back to abstract when no summary exists.
    """
    lines = []
    for p in papers:
        summary = summaries.get(p.id)
        line = f"- [{p.id}] {p.title} ({p.year or 'N/A'})"

        # Extract structured info from PaperSummary or dict
        s = None
        if summary and hasattr(summary, "research_problem"):
            s = summary
        elif summary and isinstance(summary, dict):
            s = summary

        if s:
            from core.paper_search_settings_store import (
                paper_capability_source, source_capability_enabled,
            )
            from core.reading_policy import summary_has_full_text
            if (not summary_has_full_text(summary)
                    and not source_capability_enabled(
                        paper_capability_source(p, "abstract"), "abstract"
                    )[0]):
                s = None
        if s:
            problem = getattr(s, "research_problem", None) or (s.get("research_problem", "") if isinstance(s, dict) else "")
            methodology = getattr(s, "methodology", None) or (s.get("methodology", "") if isinstance(s, dict) else "")
            findings = getattr(s, "key_findings", None) or (s.get("key_findings", []) if isinstance(s, dict) else [])
            contributions = getattr(s, "contributions", None) or (s.get("contributions", []) if isinstance(s, dict) else [])
            limitations = getattr(s, "limitations", None) or (s.get("limitations", []) if isinstance(s, dict) else [])

            if problem:
                line += f"\n  Problem: {problem[:200]}"
            if methodology:
                line += f"\n  Method: {methodology[:150]}"
            if findings and isinstance(findings, list):
                line += f"\n  Findings: {'; '.join(str(f) for f in findings[:3])[:200]}"
            if contributions and isinstance(contributions, list):
                line += f"\n  Contributions: {'; '.join(str(c) for c in contributions[:3])[:200]}"
            if limitations and isinstance(limitations, list):
                line += f"\n  Limitations: {'; '.join(str(l) for l in limitations[:2])[:150]}"
        elif paper_abstract_text(p):
            line += f"\n  Abstract: {paper_abstract_text(p)[:300]}"

        lines.append(line)
    return "\n".join(lines)


async def _write_section(prompt_template: str, lang_suffix: str = "", **kwargs) -> str:
    """Call LLM to write a section."""
    llm = get_llm("light")
    prompt = prompt_template.format(**kwargs) + lang_suffix
    try:
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        return resp.content.strip()
    except Exception as e:
        logger.error("Section writing failed: %s", e)
        return f"[Writing failed: {e}]"


def _validate_citations(text: str, valid_ids: set[str]) -> str:
    """Remove or mark invalid citations (DESIGN D-015 anti-hallucination)."""
    # Find all [xxx] patterns that look like citations
    citation_pattern = re.compile(r'\[([^\]]+)\]')
    
    def replace_citation(match):
        cid = match.group(1).strip()
        if cid in valid_ids:
            return match.group(0)
        # Check if it looks like a paper ID
        if cid.startswith("doi:") or cid.startswith("hash:"):
            return "[CITATION NEEDED]"
        return match.group(0)
    
    return citation_pattern.sub(replace_citation, text)

async def _review_and_revise(text: str, topic: str, valid_ids: set[str]) -> str:
    """Combined self-review + rewrite in a single LLM call."""
    llm = get_llm("light")
    prompt = REVIEW_AND_REVISE_PROMPT.format(
        topic=topic,
        draft=text,
        valid_ids="\n".join(f"- {pid}" for pid in list(valid_ids)[:30]),
    )
    try:
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        return resp.content.strip()
    except Exception as e:
        logger.error("Review-and-revise failed: %s", e)
        return text


def _identify_gaps(papers: list[Paper], summaries: dict[str, PaperSummary], topic: str) -> str:
    """Identify research gaps from the papers' limitations and future work."""
    gaps = []
    for p in papers:
        s = summaries.get(p.id)
        if not s:
            continue
        lims = getattr(s, "limitations", None) if hasattr(s, "limitations") else (s.get("limitations", []) if isinstance(s, dict) else [])
        if lims and isinstance(lims, list):
            for lim in lims[:2]:
                gaps.append(f"- [{p.id}] {lim}")
        fw = getattr(s, "future_work", None) if hasattr(s, "future_work") else (s.get("future_work", []) if isinstance(s, dict) else [])
        if fw and isinstance(fw, list):
            for w in fw[:1]:
                gaps.append(f"- [{p.id}] Future: {w}")

    if gaps:
        return "\n".join(gaps[:15])
    return "Limited explicit gap analysis available in the surveyed literature."

def _paper_link(p: Paper) -> str | None:
    """Pick the best public URL for a paper (D-091 citation rendering)."""
    # Prefer a landing-page URL from the per-source map over a raw PDF link.
    if p.urls:
        for src in ("doi", "openalex", "arxiv", "crossref", "europepmc",
                    "doaj", "semantic_scholar", "pubmed"):
            u = p.urls.get(src)
            if u and isinstance(u, str) and u.startswith(("http://", "https://")):
                return u
        first = next(iter(p.urls.values()), None)
        if isinstance(first, str) and first.startswith(("http://", "https://")):
            return first
    if p.doi:
        return f"https://doi.org/{p.doi.lstrip('/')}"
    if p.pdf_url and p.pdf_url.startswith(("http://", "https://")):
        return p.pdf_url
    return None


def _render_citations_as_titles(text: str, paper_map: dict[str, Paper]) -> str:
    """Replace [paper_id] citations with `Title ([Link](url))` (D-091).

    Keeps the anti-hallucination guarantees of _validate_citations (which ran
    before this and already stripped invalid ids to [CITATION NEEDED]); here
    we only rewrite the surviving valid [paper_id] tokens. Unknown tokens
    (e.g. [CITATION NEEDED]) are left untouched. Longer ids are matched first
    so a prefix id can't shadow a longer one.
    """
    if not paper_map:
        return text
    ids = sorted((i for i in paper_map.keys() if i), key=len, reverse=True)
    if not ids:
        return text
    pattern = re.compile(r"\[(" + "|".join(re.escape(i) for i in ids) + r")\]")

    def repl(m: re.Match) -> str:
        p = paper_map.get(m.group(1))
        if not p:
            return m.group(0)
        title = (p.title or m.group(1)).strip()
        link = _paper_link(p)
        if link:
            return f"{title} ([Link]({link}))"
        return title

    return pattern.sub(repl, text)
