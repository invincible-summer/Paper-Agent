"""Shared structure textutils tests (offline, pure functions)."""

from __future__ import annotations

from tools.pdf.structure._textutils import (
    REFERENCE_HEADER_RE,
    merge_references,
    pdf_fingerprint,
    split_out_references,
)
from tools.pdf.structure.models import Section


def test_merge_references_groups_entries():
    lines = [
        "[1] Smith et al. A paper about cats. 2020.",
        "continued line",
        "[2] Jones. Dogs. 2021.",
        "3. Lee, Birds (2019).",
    ]
    refs = merge_references(lines)
    assert len(refs) == 3
    assert "cats" in refs[0] and "continued" in refs[0]
    assert "Dogs" in refs[1]
    assert "Birds" in refs[2]


def test_merge_references_empty():
    assert merge_references([]) == []
    assert merge_references(["", "  "]) == []


def test_reference_header_regex():
    assert REFERENCE_HEADER_RE.match("References")
    assert REFERENCE_HEADER_RE.match("REFERENCES")
    assert REFERENCE_HEADER_RE.match("4. Bibliography")
    assert REFERENCE_HEADER_RE.match("参考文献")
    assert not REFERENCE_HEADER_RE.match("References and related work")  # trailing text


def test_split_out_references():
    secs = [
        Section(title="Introduction", text="intro"),
        Section(title="References", text="[1] Foo.\n[2] Bar."),
        Section(title="Appendix", text="appendix"),
    ]
    kept, refs = split_out_references(secs)
    assert [s.title for s in kept] == ["Introduction", "Appendix"]
    assert refs == ["[1] Foo.", "[2] Bar."]


def test_pdf_fingerprint_stable_and_sensitive(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"hello pdf body")
    b.write_bytes(b"hello pdf body")
    assert pdf_fingerprint(str(a)) == pdf_fingerprint(str(b))
    b.write_bytes(b"different body")
    assert pdf_fingerprint(str(a)) != pdf_fingerprint(str(b))
