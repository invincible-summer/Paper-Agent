"""Tests for the GB/T 7714 renderer + bibtex enrichment merge."""
from __future__ import annotations

from core.models import Paper
from tools.export.bibtex import paper_to_bibtex
from tools.export.gbt7714 import paper_to_gbt7714, papers_to_gbt7714


def _paper(**kw):
    base = dict(id="x", title="Graph Neural Networks for Recommender Systems",
                authors=["Smith, John", "Wang, Li"], year=2023,
                venue="ACM Computing Surveys", doi="10.1145/3568022")
    base.update(kw)
    return Paper(**base)


def test_gbt7714_basic_journal_entry():
    line = paper_to_gbt7714(_paper(), 1)
    assert line.startswith("[1] Smith J, Wang L.")
    assert "[J]." in line
    assert "ACM Computing Surveys, 2023." in line
    assert "DOI: 10.1145/3568022." in line


def test_gbt7714_author_truncation_et_al():
    p = _paper(authors=["Smith, John", "Wang, Li", "Chen, Hong", "Zhao, Lei"])
    line = paper_to_gbt7714(p, 2)
    assert "Smith J, Wang L, Chen H, et al." in line
    assert "Zhao" not in line


def test_gbt7714_conference_and_preprint_codes():
    conf = _paper(venue="Proceedings of the Web Conference")
    assert "[C]." in paper_to_gbt7714(conf, 1)
    preprint = _paper(venue="")
    assert "[EB/OL]." in paper_to_gbt7714(preprint, 1)


def test_gbt7714_enrichment_merges_volume_issue_pages():
    extra = {"volume": "55", "issue": "4", "page": "1-35"}
    line = paper_to_gbt7714(_paper(), 1, extra)
    assert "55(4): 1-35." in line


def test_gbt7714_numbered_list():
    text = papers_to_gbt7714([_paper(), _paper(id="y", title="Another Study")])
    assert text.startswith("[1]")
    assert "\n[2]" in text


def test_bibtex_enrichment_fields():
    entry = paper_to_bibtex(_paper(), {"volume": "55", "issue": "4",
                                       "page": "1-35", "publisher": "ACM"})
    assert "volume = {55}" in entry
    assert "number = {4}" in entry
    assert "pages = {1-35}" in entry
    assert "publisher = {ACM}" in entry
    # without enrichment: no such fields, entry unchanged
    plain = paper_to_bibtex(_paper())
    assert "volume" not in plain
