"""LangGraph shared state definition (DESIGN chapter 4)."""

from __future__ import annotations

from typing import Any, TypedDict

from core.models import Paper, PaperSummary


class ResearchState(TypedDict, total=False):
    # --- user input ---
    topic: str
    user_conception: str          # user's initial research idea
    language: str                 # "zh" | "en" | "both"
    field_profile: str            # selected field profile key
    active_fields: list[str]      # fields to extract in Reader Agent
    custom_fields: list[dict]     # user-defined custom fields

    # --- search phase ---
    search_queries: list[str]
    subtopics: list[str]
    papers: list[Paper]
    reference_papers: list[Paper]       # local-ranked but not LLM-scored (DESIGN D-028)
    reserve_papers: list[Paper]         # search layer: X papers beyond core (DESIGN D-035)

    # --- analysis phase ---
    paper_summaries: dict[str, PaperSummary]
    citation_graph_data: dict[str, Any]   # serialized graph
    topic_clusters: dict[str, list[str]]

    # --- writing phase ---
    literature_review: str
    research_directions: list[dict]        # Topic Advisor output
    selected_direction: str
    paper_framework: str                    # Framework Advisor output
    review_notes: list[dict]                # Reviewer output

    # --- flow control ---
    current_phase: str
    user_feedback: dict[str, Any]           # feedback from human-in-the-loop
    errors: list[dict[str, str]]            # error log entries
