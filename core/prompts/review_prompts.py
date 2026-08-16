"""LLM prompts for Review Synthesizer (DESIGN D-015).

Literature review writing + self-review + anti-hallucination.
"""


SECTION_WRITING_PROMPT = """You are writing a literature review section for the research topic: {topic}

This section covers the theme: "{cluster_label}"

Papers in this cluster (use ONLY these, cite by [paper_id]):
{papers_list}

Write a comprehensive review paragraph for this cluster. Requirements:
- Compare and contrast the papers (methods, findings, evolution)
- Group papers by sub-themes or chronological progression within this section
- Every factual claim MUST cite a paper using [paper_id] notation
- Do NOT reference any paper not listed above
- Write in academic style, 200-400 words
- If a claim cannot be supported by the listed papers, write [CITATION NEEDED]

Write the section now:
"""


INTRODUCTION_PROMPT = """You are writing the introduction of a literature review for: {topic}

User's research conception: {conception}

Available papers (cite by [paper_id]):
{papers_list}

Write an introduction paragraph (150-250 words) that:
- Provides background on the research field
- Explains why this topic matters
- Previews the structure of the review
- Cites relevant papers using [paper_id]
- Academic style

Write now:
"""


CROSS_TOPIC_PROMPT = """You are writing the cross-topic analysis section of a literature review for: {topic}

Topics covered in previous sections:
{topics_summary}

Key papers for cross-reference:
{papers_list}

Write a section (200-300 words) that:
- Compares different research approaches across topics
- Identifies connections between seemingly separate lines of research
- Highlights tensions or debates between different approaches
- Cites papers using [paper_id]

Write now:
"""


CONCLUSION_PROMPT = """You are writing the conclusion of a literature review for: {topic}

Previous sections covered these themes:
{topics_summary}

Identified research gaps:
{gaps}

User's conception: {conception}

Write a conclusion paragraph (150-250 words) that:
- Summarizes the key findings across all topics
- Identifies research gaps and underexplored areas
- Suggests potential future directions
- Cites papers using [paper_id] where applicable

Write now:
"""


SELF_REVIEW_PROMPT = """You are reviewing a draft literature review section.

Research topic: {topic}

Draft text:
---
{draft}
---

Available papers (valid citation IDs):
{valid_ids}

Review checklist:
1. Logic: Are transitions between paragraphs smooth?
2. Coverage: Are all important papers cited? Any cluster missing?
3. Citation accuracy: Do all [paper_id] references match the valid IDs?
4. Depth: Is the comparison/contrast analysis sufficient?
5. Academic tone: Is the language appropriate?

Output in JSON format:
{{
  "issues": [
    {{"type": "logic|coverage|citation|depth|tone", "description": "what is wrong", "suggestion": "how to fix"}},
    ...
  ],
  "invalid_citations": ["list of [paper_id] that don't exist in valid_ids"],
  "overall_quality": "excellent|good|needs_improvement",
  "rewrite_needed": true_or_false
}}

Output ONLY the JSON.
"""


REWRITE_PROMPT = """You are revising a literature review section based on reviewer feedback.

Research topic: {topic}

Original draft:
---
{draft}
---

Issues to fix:
{issues}

Invalid citations to remove or replace:
{invalid_citations}

Rewrite the section fixing all issues. Keep the same structure but improve quality.
Only cite papers from the valid list below:
{valid_ids}

Write the revised section:
"""


REVIEW_AND_REVISE_PROMPT = """You are reviewing and revising a draft literature review section.

Research topic: {topic}

Draft text:
---
{draft}
---

Available papers (valid citation IDs only):
{valid_ids}

Review the draft and rewrite it in ONE step. Fix:
1. Logic: Smooth transitions between paragraphs
2. Coverage: All important papers cited
3. Citation accuracy: All [paper_id] references must match the valid IDs above; remove invalid ones
4. Depth: Sufficient comparison/contrast analysis
5. Academic tone: Appropriate language

Output ONLY the revised section text (no JSON, no metadata, no preamble):
"""