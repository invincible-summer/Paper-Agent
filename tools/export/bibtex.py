"""Phase 3: BibTeX export for the personal paper library (pure functions).

Paper -> @article{...} (or @inproceedings/@book) entry. No I/O, no API key.
Used by the export endpoint and the chat/structured export buttons.
"""
from __future__ import annotations

import re
import unicodedata

from core.models import Paper

# Characters that must be escaped in BibTeX field values.
_BIBTEX_SPECIAL = {
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape_bibtex(value: str) -> str:
    """Escape BibTeX-special characters and strip control chars."""
    if not value:
        return ""
    out = []
    for ch in str(value):
        if ch in _BIBTEX_SPECIAL:
            out.append(_BIBTEX_SPECIAL[ch])
        elif unicodedata.category(ch).startswith("C"):
            continue  # drop control chars
        else:
            out.append(ch)
    return "".join(out)


_FIRST_WORD_RE = re.compile(r"[A-Za-z]+")


def _first_author_lastname(authors: list[str]) -> str:
    """Last name of the first author, ASCII-safe. Empty if none."""
    if not authors:
        return "anon"
    first = str(authors[0]).strip()
    if not first:
        return "anon"
    # "Smith, John" -> "Smith"; "John Smith" -> "Smith"
    if "," in first:
        lastname = first.split(",", 1)[0]
    else:
        parts = first.split()
        lastname = parts[-1] if parts else first
    # ASCII-fold so the cite key is portable.
    lastname = unicodedata.normalize("NFKD", lastname)
    lastname = "".join(c for c in lastname if not unicodedata.combining(c))
    lastname = re.sub(r"[^A-Za-z]", "", lastname)
    return lastname.lower() or "anon"


def make_cite_key(paper: Paper) -> str:
    """Stable cite key: firstauthorlastname + year + titlefirstword."""
    author = _first_author_lastname(paper.authors)
    year = str(paper.year) if paper.year else "nd"
    title_word = ""
    if paper.title:
        m = _FIRST_WORD_RE.search(paper.title)
        if m:
            title_word = m.group(0).lower()
    return f"{author}{year}{title_word}"


def _entry_type(paper: Paper) -> str:
    """Pick the entry type from venue hints (default article)."""
    venue = (paper.venue or "").lower()
    if any(k in venue for k in ("proceedings", "conference", "workshop", "symposium")):
        return "inproceedings"
    if any(k in venue for k in ("book", "monograph")):
        return "book"
    return "article"


def _field(label: str, value: str | None) -> str:
    """Format a non-empty field as `  label = {value},`."""
    if not value:
        return ""
    return f"  {label} = {{{escape_bibtex(value)}}},\n"


def paper_to_bibtex(paper: Paper, extra: dict | None = None) -> str:
    """Convert one Paper to a single BibTeX entry string.

    `extra` is an optional Crossref enrichment record (volume/issue/page/
    publisher) merged in when present.
    """
    key = make_cite_key(paper)
    etype = _entry_type(paper)
    authors = " and ".join(str(a) for a in paper.authors if str(a).strip())
    lines: list[str] = [f"@{etype}{{{key},"]
    lines.append(f"  title = {{{escape_bibtex(paper.title)}}},")
    if authors:
        lines.append(f"  author = {{{escape_bibtex(authors)}}},")
    if paper.year:
        lines.append(f"  year = {{{paper.year}}},")
    venue = (extra or {}).get("venue") or paper.venue
    if venue:
        label = "booktitle" if etype == "inproceedings" else "journal"
        lines.append(f"  {label} = {{{escape_bibtex(venue)}}},")
    if extra:
        if extra.get("volume"):
            lines.append(f"  volume = {{{escape_bibtex(extra['volume'])}}},")
        if extra.get("issue"):
            lines.append(f"  number = {{{escape_bibtex(extra['issue'])}}},")
        if extra.get("page"):
            lines.append(f"  pages = {{{escape_bibtex(extra['page'])}}},")
        if extra.get("publisher"):
            lines.append(f"  publisher = {{{escape_bibtex(extra['publisher'])}}},")
    if paper.doi:
        lines.append(f"  doi = {{{paper.doi}}},")
    url = _paper_url(paper)
    if url:
        lines.append(f"  url = {{{escape_bibtex(url)}}},")
    body = "\n".join(lines[:-1]) + "\n" + lines[-1]
    return body + "}\n"


def papers_to_bibtex(papers: list[Paper], extras: dict[str, dict] | None = None) -> str:
    """Convert many papers; dedupe cite keys with a numeric suffix.

    extras: doi -> Crossref enrichment record (optional).
    """
    extras = extras or {}
    seen: dict[str, int] = {}
    out: list[str] = []
    for p in papers:
        entry = paper_to_bibtex(p, extras.get(p.doi or "", {}))
        key = make_cite_key(p)
        if key in seen:
            seen[key] += 1
            entry = entry.replace(f"{{{key},", f"{{{key}{seen[key]},", 1)
        else:
            seen[key] = 0
        out.append(entry)
    return "\n".join(out)


def _paper_url(paper: Paper) -> str | None:
    """Best available URL for the paper (doi.org > source urls > pdf_url)."""
    if paper.doi:
        return f"https://doi.org/{paper.doi}"
    for url in (paper.urls or {}).values():
        if url:
            return url
    return paper.pdf_url
