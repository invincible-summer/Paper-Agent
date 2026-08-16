"""Prompts for the ask_papers cross-paper Q&A tool.

Anti-hallucination mirrors the review agent: the model may only cite the
[paper_id] tokens that were injected with retrieved passages, and a post-pass
strips anything else to [CITATION NEEDED].
"""
from core.prompts.registry import register

QUERY_REWRITE_PROMPT = register("qa.query_rewrite", 1, """Rewrite the user's question into a retrieval-friendly search query for a hybrid (vector + BM25) academic paper index.

Question: {query}

Rules:
- Output 1-2 lines of plain query text, nothing else (no quotes, no prefixes).
- Extract the core academic concepts; expand with standard English terminology and keep any Chinese terms that are field-specific.
- Drop conversational filler ("帮我讲讲", "what do you think about", etc.).
- If the question is already a clean keyword query, return it unchanged.
""")

QA_PROMPT = register("qa.answer", 1, """You answer a research question using ONLY the passages retrieved from papers in the user's current session. Each passage is tagged with its source [paper_id].

Question: {query}

Retrieved passages (cite by [paper_id]; do NOT invent ids):
{context}

## Depth calibration — match the answer to the question type
- Factual lookup (e.g. "用了什么数据集", "发表在哪一年", "作者是谁"): answer directly in 2-4 sentences.
- Open-ended / survey-type questions (e.g. "这篇文章研究的是什么方向", "这几篇有什么共同局限", "这个领域有哪些方法") — the default for anything not a simple lookup: write a FULL, structured answer that uses every relevant passage:
  1. Open with a 2-3 sentence direct answer.
  2. Then organized sections (### headings or bold leads) chosen from what the passages actually support: research positioning, taxonomy/framework, methods, datasets & metrics, key findings, limitations, open problems.
  3. Preserve specifics: method names, dataset names, metric values, category names with their definitions, concrete numbers. NEVER compress these away into vague one-line summaries — losing them is an information-loss failure.
  4. Close with what the evidence does NOT cover, if anything.
- Comparative questions: one bullet/row per paper, parallel structure, so differences are visible.

## Hard rules
- Answer in the user's language.
- Every factual claim MUST cite a passage using [paper_id] drawn ONLY from the ids above. Do not invent ids.
- If the passages do not support an answer (or only part of one), say so plainly and state what IS supported, rather than guessing.
- Use markdown where it helps (bold terms, headings, bullet lists, tables).

Answer now:
""")
