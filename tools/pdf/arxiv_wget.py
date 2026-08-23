"""Retired compatibility boundary for the old arXiv PDF adapter."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArxivFetchResult:
    ok: bool = False
    path: str | None = None
    error_code: str = "remote_fulltext_retired"
    bytes_written: int = 0
    content_type: str = ""


async def fetch_arxiv_pdf(*_args, destination: str | Path | None = None, **_kwargs) -> ArxivFetchResult:
    return ArxivFetchResult()
