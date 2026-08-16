"""explain_element tool tests (offline; global paper_elements table seeded).

The tool reads one element's full multimodal detail (VLM understanding +
Docling extract + asset URL) from the global paper_elements table, gated by
session membership so a paper not in the conversation can't be probed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import agents.tools_impl as ti
from core.config import get_settings
from core.models import Paper
from tools.storage.database import Database


def _seed_element(db_path: str, eid="P1::figure::1", kind="figure", **kw):
    base = dict(
        element_id=eid, kind=kind, ordinal=1, page=3, section="Method",
        caption="Architecture diagram", bbox=(1.0, 2.0, 3.0, 4.0),
        asset_path=f"data/assets/P1/{kind}_1.png", image_hash="h1",
        docling_extract={}, understanding={"description": "encoder-decoder"},
    )
    base.update(kw)
    db = Database(db_path)
    db.save_elements("P1", [SimpleNamespace(**base)], "fp1")
    db.close()
    return base


def _session(paper_ids=("P1",)):
    s = ti.ChatSession()
    s.papers = [Paper(id=pid, title=f"Title {pid}") for pid in paper_ids]
    return s


def test_explain_element_returns_full_detail(monkeypatch, tmp_path):
    db_path = str(tmp_path / "e.db")
    monkeypatch.setattr(get_settings().storage, "sqlite_path", db_path)
    _seed_element(db_path)

    result = asyncio.run(ti._tool_explain_element(
        {"element_id": "P1::figure::1"}, _session(), None))
    assert not result.is_error
    el = result.data["element"]
    assert el["kind"] == "figure"
    assert el["page"] == 3
    assert el["caption"] == "Architecture diagram"
    assert el["understanding"] == {"description": "encoder-decoder"}
    assert el["asset_url"] == "/api/v1/elements/assets/P1/figure_1.png"


def test_paper_id_parsed_from_element_id(monkeypatch, tmp_path):
    db_path = str(tmp_path / "e.db")
    monkeypatch.setattr(get_settings().storage, "sqlite_path", db_path)
    _seed_element(db_path, eid="P1::table::2", kind="table",
                  docling_extract={"markdown": "|a|b|"}, asset_path="data/assets/P1/table_2.png")

    # No explicit paper_id — must be parsed from element_id.
    result = asyncio.run(ti._tool_explain_element(
        {"element_id": "P1::table::2"}, _session(), None))
    assert not result.is_error
    el = result.data["element"]
    assert el["docling_extract"]["markdown"] == "|a|b|"
    assert el["asset_url"] == "/api/v1/elements/assets/P1/table_2.png"


def test_missing_element_id_validation_error(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings().storage, "sqlite_path", str(tmp_path / "e.db"))
    result = asyncio.run(ti._tool_explain_element({}, _session(), None))
    assert result.is_error
    assert result.error_code == "VALIDATION_ERROR"
    assert "element_id" in result.error["message"]


def test_paper_not_in_session_blocked(monkeypatch, tmp_path):
    """Session isolation: an element whose paper isn't in the conversation is refused."""
    db_path = str(tmp_path / "e.db")
    monkeypatch.setattr(get_settings().storage, "sqlite_path", db_path)
    _seed_element(db_path)

    result = asyncio.run(ti._tool_explain_element(
        {"element_id": "P1::figure::1"}, _session(paper_ids=("OTHER",)), None))
    assert result.is_error
    assert result.error_code == "NO_PAPERS"


def test_element_not_found_partial(monkeypatch, tmp_path):
    db_path = str(tmp_path / "e.db")
    monkeypatch.setattr(get_settings().storage, "sqlite_path", db_path)
    _seed_element(db_path)  # P1::figure::1 exists

    # Ask for an element id that doesn't exist.
    result = asyncio.run(ti._tool_explain_element(
        {"element_id": "P1::figure::99"}, _session(), None))
    assert result.status == "partial"
    assert result.data["element"] is None
