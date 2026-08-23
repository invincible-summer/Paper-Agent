"""Diagnostics are limited to connectivity, search and valid abstracts."""
from __future__ import annotations

import asyncio

import pytest
from tools.search import diagnostics


def test_catalog_has_no_fulltext_or_auxiliary_targets():
    catalog = diagnostics.source_catalog()
    assert catalog
    for row in catalog:
        assert set(row.get("capabilities", ())) <= {"search", "abstract"}
        assert "fulltext" not in row
        assert row.get("source") not in {"unpaywall", "doi"}


def test_fulltext_diagnostic_is_rejected():
    with pytest.raises(ValueError):
        asyncio.run(diagnostics.run_platform_diagnostics(["arxiv"], "fulltext"))


def test_flattened_results_have_only_three_capabilities():
    rows = diagnostics.flatten_capability_diagnostics([{
        "source": "arxiv",
        "connectivity": {"status": "ok"},
        "search": {"status": "ok"},
        "abstract": {"status": "ok", "abstract_length": 42},
    }])
    assert {row["capability"] for row in rows} <= {"search", "abstract"}
