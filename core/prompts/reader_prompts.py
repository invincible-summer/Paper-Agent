"""LLM prompts for Reader Agent structured extraction (DESIGN D-007, D-013)."""

from __future__ import annotations


def build_extraction_prompt(
    text: str,
    active_fields: list[str],
    field_descriptions: dict[str, str] | None = None,
    part_idx: int | None = None,
    total_parts: int | None = None,
    strict: bool = False,
) -> str:
    """Build a prompt for extracting a structured summary from paper text.

    Only includes fields the user selected (DESIGN 12.3a token saving).
    Phase 0 section-aware chunked extraction:
      - part_idx/total_parts annotate which chunk this is (map-reduce).
      - strict=True hardens the prompt for the one-shot JSON retry.
    """
    if field_descriptions is None:
        field_descriptions = _DEFAULT_FIELD_DESCRIPTIONS

    fields_text = "\n".join(
        f'    "{f}": <{field_descriptions.get(f, f)}>,'
        for f in active_fields
    )

    if part_idx and total_parts and total_parts > 1:
        part_note = (
            f"NOTE: This is part {part_idx} of {total_parts} of a longer paper. "
            "Extract what is present in THIS part; fields not covered here should be "
            "empty (they will be merged with other parts later). "
            "Each section above is labelled with its [Section Name].\n\n"
        )
    else:
        part_note = ""

    retry_note = (
        "Your previous output was not valid JSON. Reply with ONLY the JSON object, "
        "starting with '{' and ending with '}', no surrounding text.\n\n"
        if strict
        else ""
    )

    # Abstract mode (single shot) is capped at the abstract budget; full-mode
    # chunks are pre-split by reader_agent so they already fit the budget.
    body = text[:ABSTRACT_BUDGET_CHARS]

    return f"""You are a research assistant analyzing an academic paper.
Extract the following information from the text.

Text (may be abstract or full paper):
---
{body}
---

Output strictly in JSON format:
{{
{fields_text}
    "references_raw": ["ref1 text", "ref2 text", ...]
}}

Rules:
- For list fields, provide a JSON array of strings.
- For string fields, provide a concise summary (2-5 sentences).
- If a field is not applicable or not found, use empty string "" or empty list [].
- references_raw: list the bibliography entries found in the text (if any).
- Output ONLY the JSON, no extra text, no code fences.
{retry_note}{part_note}"""


# Char budget for single-shot extraction (abstract mode, or a small full text).
ABSTRACT_BUDGET_CHARS = 8000


_DEFAULT_FIELD_DESCRIPTIONS: dict[str, str] = {
    "research_problem": "What problem does this paper address?",
    "methodology": "What methods/approach does the paper use?",
    "key_findings": "Main findings or results (list)",
    "contributions": "Main contributions (list)",
    "limitations": "Limitations acknowledged or apparent (list)",
    "datasets": "Datasets used (list)",
    "baselines": "Baseline methods compared against (list)",
    "theoretical_framework": "Theoretical framework adopted",
    "sample_size": "Sample size or study population",
    "interventions": "Interventions tested (list)",
    "outcomes": "Outcome measures (list)",
    "future_work": "Future work suggested (list)",
}
