"""paper_elements SQLite storage tests (offline; temp DB)."""

from __future__ import annotations

from types import SimpleNamespace

from tools.storage.database import Database


def _el(eid: str, kind: str, ordinal: int, **kw) -> SimpleNamespace:
    base = dict(
        element_id=eid, kind=kind, ordinal=ordinal, page=1, section="Method",
        caption=f"{kind} {ordinal}", bbox=(1.0, 2.0, 3.0, 4.0),
        asset_path=f"/tmp/{eid}.png", image_hash=f"h{ordinal}",
        docling_extract={}, understanding=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_save_get_roundtrip(tmp_path):
    db = Database(str(tmp_path / "e.db"))
    els = [
        _el("p::figure::1", "figure", 1, understanding={"description": "arch"}),
        _el("p::table::1", "table", 1, docling_extract={"markdown": "|a|"}),
    ]
    assert db.save_elements("p", els, "fp1") == 2
    assert db.elements_fingerprint("p") == "fp1"

    got = db.get_elements("p")
    assert len(got) == 2
    fig = next(g for g in got if g["kind"] == "figure")
    assert fig["bbox"] == [1.0, 2.0, 3.0, 4.0]            # JSON round-tripped to list
    assert fig["understanding"] == {"description": "arch"}  # parsed back to dict
    tbl = next(g for g in got if g["kind"] == "table")
    assert tbl["docling_extract"] == {"markdown": "|a|"}


def test_replace_resets_fingerprint(tmp_path):
    db = Database(str(tmp_path / "e.db"))
    db.save_elements("p", [_el("p::figure::1", "figure", 1)], "fp1")
    assert db.elements_fingerprint("p") == "fp1"
    # Re-save with a new fingerprint and an extra element — must REPLACE, not append.
    db.save_elements(
        "p",
        [_el("p::figure::1", "figure", 1), _el("p::table::1", "table", 1)],
        "fp2",
    )
    assert db.elements_fingerprint("p") == "fp2"
    assert len(db.get_elements("p")) == 2


def test_kind_filter_and_delete(tmp_path):
    db = Database(str(tmp_path / "e.db"))
    db.save_elements(
        "p",
        [_el("p::figure::1", "figure", 1), _el("p::table::1", "table", 1)],
        "fp",
    )
    figs = db.get_elements("p", kind="figure")
    assert len(figs) == 1 and figs[0]["kind"] == "figure"
    db.delete_elements("p")
    assert db.get_elements("p") == []
    assert db.elements_fingerprint("p") is None
