"""GB/T 7714-2015 (numeric) citation renderer — the format Chinese theses require.

Pure functions over Paper (+ optional Crossref enrichment dicts). Metadata
gaps are simply omitted (no fabricated volume/pages). Entry layout:

    [n] Author1, Author2, et al. Title[J]. Venue, Year, Volume(Issue): Pages. DOI

Type codes: [J] journal, [C] proceedings, [M] book, [EB/OL] online/preprint.
"""
from __future__ import annotations

import re
import unicodedata

from core.models import Paper


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text or "")


def _author_gbt(name: str) -> str:
    """'Smith, John' / 'John Smith' -> 'Smith J' (GB/T: family + initials)."""
    name = (name or "").strip()
    if not name:
        return ""
    if "," in name:
        family, given = name.split(",", 1)
    else:
        parts = name.split()
        family, given = parts[-1], " ".join(parts[:-1])
    initials = " ".join(p[0].upper() for p in re.split(r"[\s\-]+", given.strip()) if p)
    return f"{family.strip()} {initials}".strip()


def _format_authors(authors: list[str], cjk: bool) -> str:
    names = [_author_gbt(a) for a in authors if str(a).strip()]
    names = [n for n in names if n]
    if not names:
        return "佚名" if cjk else "Anon"
    if len(names) > 3:
        tail = "等" if cjk else "et al"
        return ", ".join(names[:3]) + f", {tail}"
    return ", ".join(names)


def _type_code(paper: Paper, venue: str = "") -> str:
    v = (venue or paper.venue or "").lower()
    if any(k in v for k in ("proceedings", "conference", "workshop", "symposium")):
        return "C"
    if any(k in v for k in ("book", "monograph")):
        return "M"
    if v:
        return "J"
    return "EB/OL"  # no venue at all: preprint / online resource


def _paper_url(paper: Paper) -> str:
    if paper.doi:
        return f"https://doi.org/{paper.doi.lstrip('/')}"
    for url in (paper.urls or {}).values():
        if url:
            return url
    return ""


def paper_to_gbt7714(paper: Paper, index: int, extra: dict | None = None) -> str:
    """One GB/T 7714 numeric entry. `extra` is the Crossref enrichment record."""
    extra = extra or {}
    cjk = _has_cjk(paper.title or "")
    authors = _format_authors(paper.authors, cjk)
    venue = extra.get("venue") or (paper.venue or "").strip()
    code = _type_code(paper, venue)
    title = (paper.title or "").strip().rstrip(".")

    parts = [f"[{index}] {authors}. {title}[{code}]."]
    pub_bits: list[str] = []
    if venue:
        pub_bits.append(venue)
    if paper.year:
        pub_bits.append(str(paper.year))
    vol = extra.get("volume", "")
    issue = extra.get("issue", "")
    if vol:
        pub_bits.append(f"{vol}({issue})" if issue else vol)
    if pub_bits:
        parts.append(", ".join(pub_bits))
    page = extra.get("page", "")
    if page:
        parts[-1] = parts[-1] + f": {page}"
    if len(parts) > 1:
        parts[-1] = parts[-1].rstrip(".") + "."
    if paper.doi:
        parts.append(f"DOI: {paper.doi}.")
    elif _paper_url(paper):
        parts.append(f"{_paper_url(paper)}.")
    # normalize internal whitespace (unicodedata-safe)
    return unicodedata.normalize("NFC", " ".join(parts))


def papers_to_gbt7714(papers: list[Paper], extras: dict[str, dict] | None = None) -> str:
    """Numbered GB/T 7714 reference list. extras: doi -> enrichment record."""
    extras = extras or {}
    lines = []
    for i, p in enumerate(papers, 1):
        extra = extras.get(p.doi or "", {})
        lines.append(paper_to_gbt7714(p, i, extra))
    return "\n".join(lines) + "\n"
