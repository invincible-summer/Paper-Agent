"""Shared text utilities for structure parsers.

Section-header and reference-list heuristics reused by both the Docling backend
(reference-list splitting) and the PyMuPDF fallback (section + reference
detection). Kept here so the two backends stay consistent.
"""

from __future__ import annotations

import hashlib
import re

# Matches a standalone References / Bibliography / Works Cited / 参考文献 header.
REFERENCE_HEADER_RE = re.compile(
    r"^(?:\d+\.?\s*)?(references|bibliography|works\s+cited|参考文献)\s*$",
    re.IGNORECASE,
)

# Matches the start of a numbered bibliography entry: "[1] ..." or "1. ...".
_REF_ENTRY_START_RE = re.compile(r"^\[?\d+\]?\.?\s")


def pdf_fingerprint(pdf_path: str) -> str:
    """SHA-256 of a PDF's bytes — the cache-version key for element storage.

    A changed PDF (different bytes) yields a different fingerprint, so element
    extraction is re-run only when the document actually changed. Robust to
    path/metadata renames.
    """
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def merge_references(lines: list[str]) -> list[str]:
    """Merge loose reference-section lines into discrete entries.

    Heuristic: a new entry starts at a line beginning with [N] or N. Continuation
    lines are appended to the current entry.
    """
    refs: list[str] = []
    current = ""
    for line in lines:
        if not line.strip():
            continue
        if _REF_ENTRY_START_RE.match(line) and current:
            refs.append(current.strip())
            current = line
        else:
            current += " " + line
    if current.strip():
        refs.append(current.strip())
    return refs


def split_out_references(sections: list) -> tuple[list, list[str]]:
    """Pull a References section out of a section list.

    Returns (sections_without_references, references_list). Only the first
    section whose title matches REFERENCE_HEADER_RE is treated as references.
    """
    out: list = []
    references: list[str] = []
    for sec in sections:
        if not references and REFERENCE_HEADER_RE.match((sec.title or "").strip()):
            references = merge_references((sec.text or "").split("\n"))
        else:
            out.append(sec)
    return out, references
