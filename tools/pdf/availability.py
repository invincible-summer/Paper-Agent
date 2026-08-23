"""Retired network-paper PDF availability API.

Kept as a tiny import-compatible shim for old extensions and migrations.  It
never probes, resolves, downloads, or persists a network full-text status.
"""
from __future__ import annotations

FULLTEXT_STATUS_UNKNOWN = "unknown"
FULLTEXT_STATUS_AVAILABLE = "unknown"
FULLTEXT_STATUS_UNAVAILABLE = "unknown"


def _retired(*_args, **_kwargs):
    return {}


async def verify_paper_fulltext(*_args, **_kwargs):
    return None


async def verify_papers_fulltext(*_args, **_kwargs):
    return {}
