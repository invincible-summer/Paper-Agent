"""Structure-aware text chunking (replaces naive fixed-size slicing).

Rules:
  1. Split on paragraph boundaries (blank lines) first.
  2. Pack whole paragraphs into chunks up to `size` chars.
  3. A paragraph larger than `size` is split at sentence boundaries
     (。！？.!?；; followed by space/CJK), never mid-sentence.
  4. Overlap is expressed as whole trailing sentences carried into the next
     chunk — never a raw character slice.
"""
from __future__ import annotations

import re

_PARA_RE = re.compile(r"\n\s*\n+")
# Sentence enders: CJK + latin, optionally followed by quotes/brackets.
_SENT_RE = re.compile(r"(?<=[。！？!?；;.])\s*|(?<=[。！？!?；])")
_MIN_TAIL = 40  # a trailing piece shorter than this is merged back


def split_sentences(text: str) -> list[str]:
    """Split text into sentences (CJK + latin aware)."""
    parts = [p.strip() for p in _SENT_RE.split(text or "") if p and p.strip()]
    return parts


def smart_chunks(text: str, size: int = 900, overlap_sentences: int = 1) -> list[str]:
    """Chunk text on paragraph/sentence boundaries. Returns [] for empty text."""
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in _PARA_RE.split(text) if p.strip()]
    # Break oversized paragraphs into sentence groups.
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= size:
            units.append(para)
            continue
        buf = ""
        for sent in split_sentences(para):
            if len(sent) > size:
                # Last resort: a single sentence longer than the block size is
                # hard-sliced (text without punctuation would never split).
                if buf:
                    units.append(buf)
                    buf = ""
                units.extend(sent[i:i + size] for i in range(0, len(sent), size))
                continue
            if buf and len(buf) + len(sent) + 1 > size:
                units.append(buf)
                buf = sent
            else:
                buf = f"{buf} {sent}".strip() if buf else sent
        if buf:
            units.append(buf)

    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) + 2 > size:
            chunks.append(current)
            current = unit
        else:
            current = f"{current}\n\n{unit}" if current else unit
    if current:
        chunks.append(current)

    # Sentence-level overlap: prepend the last sentence(s) of chunk i to i+1.
    if overlap_sentences > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            tail_sents = split_sentences(chunks[i - 1])[-overlap_sentences:]
            prefix = " ".join(tail_sents)
            piece = f"{prefix} {chunks[i]}" if prefix else chunks[i]
            overlapped.append(piece)
        chunks = overlapped

    # Merge a tiny trailing chunk into its predecessor.
    if len(chunks) > 1 and len(chunks[-1]) < _MIN_TAIL:
        chunks[-2] = chunks[-2] + "\n\n" + chunks[-1]
        chunks.pop()
    return chunks
