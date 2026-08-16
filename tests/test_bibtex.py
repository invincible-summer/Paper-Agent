"""Phase 3: BibTeX export tests (pure functions, no I/O)."""

from __future__ import annotations

from core.models import Paper
from tools.export.bibtex import (
    escape_bibtex,
    make_cite_key,
    paper_to_bibtex,
    papers_to_bibtex,
)


def _paper(**kw):
    base = dict(id="x", title="A Great Paper", authors=["Smith, John"], year=2020)
    base.update(kw)
    return Paper(**base)


def test_escape_special_chars():
    assert escape_bibtex("a & b % c") == r"a \& b \% c"
    assert escape_bibtex("x_y") == r"x\_y"
    assert escape_bibtex("") == ""
    # control chars dropped
    assert escape_bibtex("a\x00b") == "ab"


def test_cite_key_author_year_word():
    p = _paper(title="Attention Mechanism", authors=["Vaswani, Ashish"], year=2017)
    assert make_cite_key(p) == "vaswani2017attention"


def test_cite_key_lastname_extraction():
    # "John Smith" -> smith
    p = _paper(authors=["John Smith"], title="Foo")
    assert make_cite_key(p).startswith("smith")
    # no authors
    p2 = _paper(authors=[], title="Bar")
    key = make_cite_key(p2)
    assert "anon" in key


def test_entry_type_article_by_default():
    entry = paper_to_bibtex(_paper(venue="Nature"))
    assert entry.startswith("@article{")


def test_entry_type_inproceedings_for_conference():
    entry = paper_to_bibtex(_paper(venue="Proceedings of NeurIPS"))
    assert entry.startswith("@inproceedings{")
    assert "booktitle" in entry
    assert "journal" not in entry


def test_paper_to_bibtex_has_required_fields():
    p = _paper(
        title="On X & Y",
        authors=["Doe, Jane"],
        year=2021,
        venue="Science",
        doi="10.1000/xyz",
    )
    entry = paper_to_bibtex(p)
    assert "@article{" in entry
    assert "doe2021on" in entry
    assert "title = {On X \\& Y}" in entry
    assert "author = {Doe, Jane}" in entry
    assert "year = {2021}" in entry
    assert "journal = {Science}" in entry
    assert "doi = {10.1000/xyz}" in entry
    assert "url = {https://doi.org/10.1000/xyz}" in entry


def test_missing_year_and_doi_handled():
    p = _paper(year=None, doi=None, venue="", authors=["Lee"])
    entry = paper_to_bibtex(p)
    assert "year" not in entry
    assert "doi" not in entry
    assert "journal" not in entry
    assert "url" not in entry


def test_url_fallback_to_pdf():
    p = _paper(doi=None, pdf_url="https://example.org/paper.pdf")
    entry = paper_to_bibtex(p)
    assert "url = {https://example.org/paper.pdf}" in entry


def test_dedup_cite_keys():
    p1 = _paper(id="a", title="Alpha", authors=["Brown"])
    p2 = _paper(id="b", title="Alpha", authors=["Brown"])  # same key
    out = papers_to_bibtex([p1, p2])
    assert "brown2020alpha," in out  # first
    assert "brown2020alpha1," in out  # dedup suffix
