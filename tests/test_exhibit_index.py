"""exhibit_index: figure/table caption extraction (zero LLM)."""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.models import Paper, PaperSummary
from core.tool_protocol import ErrorCode
from agents.session import ChatSession
from agents.tools_impl import _tool_exhibit_index, validate_args
from tools.pdf.caption import extract_captions

TEXT = """## Abstract
We study X.

## 1 Introduction
Intro text. As shown in Figure 1, the model works on many tasks.

## 2 Methods
Figure 2: Architecture overview. The encoder has 6 layers and a cross-attention
module connecting the two streams end to end.

Table 1: Dataset statistics. We use three benchmarks spanning
vision and language tasks.

图 3：中文标题示例。
"""


def test_extract_finds_three_captions():
    caps = extract_captions(TEXT)
    nums = [c["num"] for c in caps]
    assert "Figure 2" in nums and "Table 1" in nums
    assert any("图" in c["num"] and c["num"].endswith("3") for c in caps)
    assert len(caps) == 3


def test_extract_ignores_inline_figure_references():
    caps = extract_captions(TEXT)
    # "As shown in Figure 1" is mid-sentence → NOT a caption start.
    assert not any(c["num"] == "Figure 1" for c in caps)


def test_extract_merges_multiline_caption():
    caps = extract_captions(TEXT)
    fig2 = next(c for c in caps if c["num"] == "Figure 2")
    assert "cross-attention" in fig2["caption"]
    assert "end to end" in fig2["caption"]  # continuation line merged


def test_extract_classifies_types():
    caps = extract_captions(TEXT)
    by_num = {c["num"]: c["type"] for c in caps}
    assert by_num["Figure 2"] == "figure"
    assert by_num["Table 1"] == "table"


def test_extract_handles_subfigure_and_dot_prefixes():
    text = "Fig. S2: supplementary plot.\nFig 1a: detail view."
    caps = extract_captions(text)
    nums = {c["num"] for c in caps}
    assert any("S2" in n for n in nums)
    assert any("1a" in n for n in nums)


def test_extract_empty_text():
    assert extract_captions("") == []
    assert extract_captions("no captions here at all") == []


def test_extract_page_passthrough():
    caps = extract_captions("Figure 1: hi.", page="4")
    assert caps[0]["page"] == "4"


# ---------------------------------------------------------------------------
# _tool_exhibit_index
# ---------------------------------------------------------------------------

def _sess(papers, summaries=None):
    s = ChatSession()
    s.papers = papers
    s.paper_summaries = summaries or {}
    return s


def test_tool_guard_no_papers():
    res = asyncio.run(_tool_exhibit_index({}, ChatSession(), None))
    assert res.is_error and res.error_code == ErrorCode.NO_PAPERS


def test_tool_extracts_from_fulltext_summary():
    p = Paper(id="p1", title="Paper One", source="openalex")
    s = _sess([p], {"p1": PaperSummary(paper_id="p1", full_text=TEXT)})
    res = asyncio.run(_tool_exhibit_index({}, s, None))
    assert not res.is_error
    groups = res.data["exhibits"]
    assert len(groups) == 1 and groups[0]["paper_id"] == "p1"
    assert len(groups[0]["captions"]) == 3


def test_tool_marks_explicit_request_without_fulltext():
    p = Paper(id="p2", title="No Fulltext", source="arxiv")
    s = _sess([p], {})  # no summary / no full text
    res = asyncio.run(_tool_exhibit_index({"paper_ids": ["p2"]}, s, None))
    assert res.status == "partial"  # nothing extracted
    assert res.data["exhibits"] == []
    assert res.data["missing_fulltext"][0]["id"] == "p2"


def test_schema_validation():
    args, err_res = validate_args("exhibit_index", {"paper_ids": ["p1"]})
    assert err_res is None and args["paper_ids"] == ["p1"]
    _, err_res = validate_args("exhibit_index", {"paper_ids": 5})
    assert err_res is not None and err_res.error_code == ErrorCode.VALIDATION_ERROR


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))


def test_tool_attachment_result_updates_frontend_status(monkeypatch, tmp_path):
    import agents.tools_impl as ti
    import tools.ingest.attachments as attachment_ingest

    aid = "d" * 32
    attachment = {"id": aid, "filename": "chart.png", "ext": "png",
                  "multimodal_status": "pending"}
    session = ChatSession(attachments=[attachment])

    async def fake_understand(item, _session, *, focus=""):
        item["multimodal_status"] = "ready"
        item["element_count"] = 1
        return {"id": aid, "status": "ready", "element_count": 1, "char_count": 42}

    class FakeDB:
        def __init__(self, *_args, **_kwargs): pass
        def get_elements(self, _paper_id):
            return [{"element_id": f"upload:{aid}::figure::1", "kind": "figure",
                     "ordinal": 1, "caption": "chart", "page": 1,
                     "asset_path": str(tmp_path / "figure.png")}]
        def close(self): pass

    monkeypatch.setattr(attachment_ingest, "ensure_attachment_understood", fake_understand)
    import tools.storage.database as database
    monkeypatch.setattr(database, "Database", FakeDB)
    result = asyncio.run(ti._tool_exhibit_index({}, session, None))
    assert result.data["attachments"] == [{
        "filename": "chart.png", "id": aid, "status": "ready",
        "element_count": 1, "char_count": 42,
    }]
