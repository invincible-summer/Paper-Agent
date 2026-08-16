"""BibTeX import — minimal tolerant .bib parser (no new dependencies).

Parses ``@type{key, field = {value} | "value", ...}`` entries produced by
Zotero / EndNote / Mendeley. Tolerant by design: unbalanced braces or unknown
entry types are skipped, never raised. Each parsed entry becomes a plain dict
the tool layer turns into a Paper.

Only the fields Paper Agent consumes are extracted: title / author / year /
doi / venue (journal or booktitle) / abstract. Everything else is ignored.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Entry types that are metadata, not real citations.
_SKIP_TYPES = {"comment", "string", "preamble", "mailto"}
_ENTRY_START = re.compile(r"@(\w+)\s*\{")
_FIELD_SPLIT = re.compile(r"\s*([\w\-]+)\s*=\s*(.*)", re.DOTALL)


def _find_entry_body(text: str, start: int) -> tuple[str, int] | None:
    """Return (body_inside_braces, index_after_closing_brace) or None."""
    depth = 1
    i = start
    n = len(text)
    while i < n and depth > 0:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return None  # unbalanced


def _split_top_commas(s: str) -> list[str]:
    """Split on commas that are at brace-depth 0 and outside double quotes."""
    parts: list[str] = []
    depth = 0
    in_str = False
    cur: list[str] = []
    for c in s:
        if c == '"' and depth == 0:
            in_str = not in_str
            cur.append(c)
        elif c == "{":
            depth += 1
            cur.append(c)
        elif c == "}":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0 and not in_str:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if cur:
        parts.append("".join(cur))
    return parts


def _strip_value(value: str) -> str:
    """Strip outer braces/quotes and collapse inner braces + whitespace."""
    v = value.strip()
    # Drop trailing comma leftover from comma-split.
    while v.endswith(","):
        v = v[:-1].strip()
    if v.startswith("{") and v.endswith("}"):
        v = v[1:-1]
    elif v.startswith('"') and v.endswith('"'):
        v = v[1:-1]
    v = v.replace("{", "").replace("}", "")
    return " ".join(v.split()).strip()


def _parse_entry(entry_type: str, body: str) -> dict:
    rec = {"entry_type": entry_type, "key": "", "title": "", "authors": [],
           "year": None, "doi": "", "venue": "", "abstract": ""}
    tokens = _split_top_commas(body)
    for idx, tok in enumerate(tokens):
        if _looks_like_field(tok):
            m = _FIELD_SPLIT.match(tok.strip())
            if not m:
                continue
            name = m.group(1).lower()
            value = _strip_value(m.group(2))
            if name == "title":
                rec["title"] = value
            elif name == "author":
                rec["authors"] = [a.strip() for a in re.split(r"\s+and\s+", value) if a.strip()]
            elif name == "year":
                ym = re.search(r"\d{4}", value)
                rec["year"] = int(ym.group(0)) if ym else None
            elif name == "doi":
                rec["doi"] = value
            elif name in ("journal", "booktitle"):
                rec["venue"] = value
            elif name == "abstract":
                rec["abstract"] = value
        elif idx == 0:
            rec["key"] = tok.strip()  # cite key (no '=' )
    return rec


def _looks_like_field(tok: str) -> bool:
    """True when the token is `name = value`; a bare cite key has no '='."""
    if "=" not in tok:
        return False
    head = tok.split("=", 1)[0]
    return bool(re.fullmatch(r"\s*[\w\-]+\s*", head))


def parse_bibtex(text: str) -> list[dict]:
    """Parse a .bib string into a list of entry dicts (tolerant, never raises)."""
    if not text or not text.strip():
        return []
    entries: list[dict] = []
    pos = 0
    while True:
        m = _ENTRY_START.search(text, pos)
        if not m:
            break
        entry_type = m.group(1).lower()
        body_res = _find_entry_body(text, m.end())
        if body_res is None:
            pos = m.end()
            continue
        body, pos = body_res
        if entry_type in _SKIP_TYPES:
            continue
        try:
            entries.append(_parse_entry(entry_type, body))
        except Exception as e:  # noqa: BLE001 — never let one bad entry abort the parse
            logger.debug("bibtex entry parse skipped: %s", e)
    return entries
