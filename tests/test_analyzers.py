"""Stage-2 analyzer tests (offline; VisionClient + cache stubbed).

Covers:
- orchestration: empty input, vision-unconfigured no-op, fan-out
- cost control: cache hits are free (don't count vs the call budget), the two
  hard caps (ELEMENTS_PER_PAPER_CAP / VISION_CALLS_PER_PAPER), prioritization
  figures > tables > formulas
- resilience: partial-failure isolation (one element failing never blocks the
  others), elements without a rendered asset are skipped cleanly
- kind-specific merge rules: table markdown (Docling preferred, VLM backfills
  only when Docling has none), formula LaTeX (VLM authoritative upgrade)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import core.multimodal.analyzers as an
from core.multimodal.analyzers import _apply_result
from tools.pdf.structure.models import PaperElement


# --- fakes ---------------------------------------------------------------------

class FakeClient:
    """Records every analyze() call; returns a per-task dict or raises."""

    def __init__(self, results=None, fail_png=None):
        self.calls: list[tuple[str, bytes]] = []
        self._results = results or {}      # task -> dict
        self._fail_png = fail_png or set()  # set of exact png byte-values to raise on

    async def analyze(self, png, task, prompt, *, prompt_version=1, expect_json=True, mime="image/png"):
        self.calls.append((task, png))
        if png in self._fail_png:
            raise RuntimeError("vlm boom")
        return self._results.get(task)


class FakeCache:
    def __init__(self, store=None):
        self.store = store or {}  # (image_hash, task, version) -> dict

    def get(self, image_hash, task, version):
        return self.store.get((image_hash, task, version))


def _fig(eid, asset=None, ihash=None) -> PaperElement:
    return PaperElement(element_id=eid, kind="figure", ordinal=int(eid[-1]) if eid[-1:].isdigit() else 0,
                        page=1, caption=f"{eid} cap", asset_path=asset, image_hash=ihash)


def _tbl(eid, asset=None, ihash=None, docling=None) -> PaperElement:
    return PaperElement(element_id=eid, kind="table", ordinal=1, page=1,
                        asset_path=asset, image_hash=ihash, docling_extract=docling or {})


def _fml(eid, asset=None, ihash=None, docling=None) -> PaperElement:
    return PaperElement(element_id=eid, kind="formula", ordinal=1, page=1,
                        asset_path=asset, image_hash=ihash, docling_extract=docling or {})


def _write_asset(tmp_path, name, payload=b"\x89PNGfake") -> str:
    p = tmp_path / name
    p.write_bytes(payload)
    return str(p)


def _patch(monkeypatch, client=None, cache=None, cap=None, budget=None):
    monkeypatch.setattr(an, "get_vision_client", lambda: client)
    monkeypatch.setattr(an, "get_vision_cache", lambda: cache or FakeCache())
    if cap is not None:
        monkeypatch.setattr(an, "ELEMENTS_PER_PAPER_CAP", cap)
    if budget is not None:
        monkeypatch.setattr(an, "VISION_CALLS_PER_PAPER", budget)


# --- orchestration -------------------------------------------------------------

def test_empty_list_is_noop(monkeypatch):
    _patch(monkeypatch, client=FakeClient())
    out = asyncio.run(an.understand_elements([]))
    assert out == []


def test_vision_unconfigured_leaves_elements_untouched(monkeypatch, tmp_path):
    asset = _write_asset(tmp_path, "f.png")
    el = _fig("p::figure::1", asset=asset, ihash="h1")
    _patch(monkeypatch, client=None)  # get_vision_client -> None
    asyncio.run(an.understand_elements([el]))
    assert el.understanding is None  # degraded: kept Docling extract / caption only


def test_figure_is_understood(monkeypatch, tmp_path):
    asset = _write_asset(tmp_path, "f.png", b"png1")
    el = _fig("p::figure::1", asset=asset, ihash="h1")
    client = FakeClient(results={"figure": {"description": "an architecture diagram"}})
    _patch(monkeypatch, client=client)
    asyncio.run(an.understand_elements([el]))
    assert len(client.calls) == 1 and client.calls[0][0] == "figure"
    assert el.understanding == {"description": "an architecture diagram"}


def test_cache_hit_is_free_and_does_not_count_vs_budget(monkeypatch, tmp_path):
    a1 = _write_asset(tmp_path, "f1.png", b"png1")
    a2 = _write_asset(tmp_path, "f2.png", b"png2")
    cached_fig = _fig("p::figure::1", asset=a1, ihash="h1")
    fresh_fig = _fig("p::figure::2", asset=a2, ihash="h2")
    cache = FakeCache(store={("h1", "figure", 1): {"description": "from cache"}})
    client = FakeClient(results={"figure": {"description": "from vlm"}})
    # budget=1: the cached element is free, so the one fresh element must still be billed.
    _patch(monkeypatch, client=client, cache=cache, budget=1)
    asyncio.run(an.understand_elements([cached_fig, fresh_fig]))
    assert cached_fig.understanding == {"description": "from cache"}
    assert fresh_fig.understanding == {"description": "from vlm"}
    assert len(client.calls) == 1  # cache hit made zero calls


# --- budget caps + prioritization ----------------------------------------------

def test_call_budget_caps_billable_calls(monkeypatch, tmp_path):
    figs = []
    for i in range(4):
        a = _write_asset(tmp_path, f"f{i}.png", f"png{i}".encode())
        figs.append(_fig(f"p::figure::{i}", asset=a, ihash=f"h{i}"))
    client = FakeClient(results={"figure": {"description": "x"}})
    _patch(monkeypatch, client=client, budget=2)
    asyncio.run(an.understand_elements(figs))
    assert len(client.calls) == 2  # hard cap on billable calls


def test_elements_cap_caps_candidates(monkeypatch, tmp_path):
    figs = []
    for i in range(4):
        a = _write_asset(tmp_path, f"f{i}.png", f"png{i}".encode())
        figs.append(_fig(f"p::figure::{i}", asset=a, ihash=f"h{i}"))
    client = FakeClient(results={"figure": {"description": "x"}})
    _patch(monkeypatch, client=client, cap=2)
    asyncio.run(an.understand_elements(figs))
    assert len(client.calls) == 2  # only 2 candidates considered at all


def test_figures_prioritized_over_tables_and_formulas(monkeypatch, tmp_path):
    # 1 of each kind; budget=1 means only the figure is billed.
    fa = _write_asset(tmp_path, "fig.png", b"fpng")
    ta = _write_asset(tmp_path, "tbl.png", b"tpng")
    ma = _write_asset(tmp_path, "fml.png", b"mpng")
    els = [
        _tbl("p::table::1", asset=ta, ihash="th"),
        _fig("p::figure::1", asset=fa, ihash="fh"),
        _fml("p::formula::1", asset=ma, ihash="mh"),
    ]
    client = FakeClient(results={"figure": {"description": "f"},
                                 "table": {"summary": "t"},
                                 "formula": {"latex": "x"}})
    _patch(monkeypatch, client=client, budget=1)
    asyncio.run(an.understand_elements(els))
    tasks = [t for t, _ in client.calls]
    assert tasks == ["figure"]  # figure wins the single budget slot


# --- resilience ----------------------------------------------------------------

def test_partial_failure_isolates_one_element(monkeypatch, tmp_path):
    a1 = _write_asset(tmp_path, "f1.png", b"png1")
    a2 = _write_asset(tmp_path, "f2.png", b"png2")
    boom = _fig("p::figure::1", asset=a1, ihash="h1")
    ok = _fig("p::figure::2", asset=a2, ihash="h2")
    client = FakeClient(results={"figure": {"description": "ok"}},
                        fail_png={b"png1"})
    _patch(monkeypatch, client=client)
    asyncio.run(an.understand_elements([boom, ok]))
    assert boom.understanding is None     # failed element degraded, not crashed
    assert ok.understanding == {"description": "ok"}  # sibling unaffected


def test_element_without_asset_is_skipped(monkeypatch, tmp_path):
    # A table Docling located but could not render (no bbox crop) has no asset.
    t = _tbl("p::table::1", asset=None, ihash=None, docling={"markdown": "|a|"})
    f = _fig("p::figure::1", asset=_write_asset(tmp_path, "f.png", b"p"), ihash="h")
    client = FakeClient(results={"figure": {"description": "f"}})
    _patch(monkeypatch, client=client)
    asyncio.run(an.understand_elements([t, f]))
    assert t.understanding is None        # skipped — no image to send
    assert len(client.calls) == 1         # the table made no call


# --- kind-specific merge rules (pure unit) -------------------------------------

def test_apply_figure_stores_full_result():
    el = _fig("p::figure::1")
    _apply_result(el, {"type": "architecture", "description": "d", "components": ["a"]})
    assert el.understanding == {"type": "architecture", "description": "d", "components": ["a"]}


def test_apply_table_backfills_markdown_when_docling_has_none():
    el = _tbl("p::table::1", docling={})
    _apply_result(el, {"markdown": "|vlm|", "summary": "s", "key_metrics": ["m"]})
    assert el.docling_extract["markdown"] == "|vlm|"  # backfilled
    assert el.understanding["summary"] == "s"


def test_apply_table_keeps_docling_markdown_over_vlm():
    el = _tbl("p::table::1", docling={"markdown": "|doc|"})
    _apply_result(el, {"markdown": "|vlm|", "summary": "s"})
    assert el.docling_extract["markdown"] == "|doc|"  # Docling TableFormer preferred
    assert el.understanding["summary"] == "s"


def test_apply_formula_upgrades_latex_to_vlm_authoritative():
    el = _fml("p::formula::1", docling={"latex": "x"})
    _apply_result(el, {"latex": r"\frac{a}{b}", "meaning": "ratio", "variables": {}})
    assert el.docling_extract["latex"] == r"\frac{a}{b}"  # VLM corrected
    assert el.understanding["meaning"] == "ratio"


def test_apply_formula_keeps_docling_latex_when_vlm_empty():
    el = _fml("p::formula::1", docling={"latex": "x"})
    _apply_result(el, {"latex": "", "meaning": "m"})
    assert el.docling_extract["latex"] == "x"  # not clobbered by empty VLM output
