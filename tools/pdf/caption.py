"""Figure/table caption extraction (zero LLM) — the text of a caption is fair
game even though the image itself is not (pure-text model).

Two entry points share one scanner:
  - extract_captions(text)            : pure, page-agnostic (full_text source)
  - extract_captions_from_pdf(path)   : page-accurate, via PyMuPDF per-page text

A caption STARTS a line with ``Figure / Fig. / Table / Tab. / 图 / 表`` followed
by a number/letter code and a separator. Continuation lines are merged until a
blank line, the next caption, or a section header. Unknown layouts simply yield
fewer captions — never raise.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Caption opener: type prefix + number/letter code + separator (. : ： or space).
_CAP_START = re.compile(
    r"^(Figure|Fig\.?|TABLE|Table|Tab\.?|图|表)\s*([A-Za-z0-9][A-Za-z0-9.\-]*)\s*[\.:：]",
)

# Section headers (to stop caption continuation + tag the current section).
_SECTION_HEADER = re.compile(
    r"^\s*#*\s*(?:\d+\.?\s*)?(abstract|introduction|background|related work|method"
    r"|methodology|approach|experiment|evaluation|result|discussion"
    r"|conclusion|acknowledgment|reference|appendix)\b",
    re.IGNORECASE,
)

_FIG_PREFIXES = {"figure", "fig"}
_TAB_PREFIXES = {"table", "tab"}


def _classify(prefix: str) -> str:
    if prefix in ("图",):
        return "figure"
    if prefix in ("表",):
        return "table"
    p = prefix.lower().rstrip(".")
    if p in _FIG_PREFIXES:
        return "figure"
    if p in _TAB_PREFIXES:
        return "table"
    return "figure"


def _scan_lines(lines: list[str], page: str) -> list[dict]:
    """Scan already-split lines for captions; returns caption dicts."""
    captions: list[dict] = []
    current_section = ""
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        m = _CAP_START.match(stripped)
        if m:
            prefix, num = m.group(1), m.group(2)
            buf = [stripped]
            j = i + 1
            while j < n:
                nxt = lines[j].strip()
                if not nxt or _CAP_START.match(nxt):
                    break
                if _SECTION_HEADER.match(nxt) and len(nxt) < 80:
                    break
                buf.append(nxt)
                j += 1
            captions.append({
                "num": f"{prefix} {num}".strip(),
                "type": _classify(prefix),
                "caption": " ".join(buf),
                "page": page,
                "section": current_section,
            })
            i = j
            continue
        if _SECTION_HEADER.match(stripped) and len(stripped) < 80:
            current_section = stripped
        i += 1
    return captions


def extract_captions(text: str, page: str = "") -> list[dict]:
    """Extract figure/table captions from a plain-text full-text string."""
    if not text:
        return []
    return _scan_lines(text.splitlines(), page)


def extract_captions_from_pdf(pdf_path: str) -> list[dict]:
    """Page-accurate caption extraction from a PDF on disk (best-effort)."""
    try:
        import fitz  # PyMuPDF
    except Exception as e:  # noqa: BLE001
        logger.debug("PyMuPDF unavailable for caption extraction: %s", e)
        return []
    out: list[dict] = []
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:  # noqa: BLE001
        logger.debug("caption PDF open failed %s: %s", pdf_path, e)
        return []
    try:
        for pno, page in enumerate(doc, start=1):
            text = page.get_text("text")
            if text:
                out.extend(_scan_lines(text.splitlines(), page=str(pno)))
    finally:
        doc.close()
    return out
