"""论文集聚合:跨会话汇总当前用户检索到的网络论文元数据。

只读 history_record 中该用户会话的 papers/candidates 字段并去重;
网络论文没有本地全文,响应仅含元数据(标题/作者/摘要/来源链接)。
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Header

from app.api.v1.auth import current_user

router = APIRouter(prefix="/papers", tags=["papers"])

_ABSTRACT_CAP = 600  # 摘要截断,前端列表展示足够
_MAX_PAPERS = 2000


def _dedup_key(paper: dict) -> tuple[str, str]:
    """DOI 优先,其次原始 id,最后规范化标题;同一论文跨会话只保留一条。"""
    doi = str(paper.get("doi") or "").strip().lower()
    if doi:
        return ("doi", doi)
    pid = str(paper.get("id") or "").strip().lower()
    if pid:
        return ("id", pid)
    title = re.sub(r"\s+", " ", str(paper.get("title") or "")).strip().lower()
    return ("title", title)


def _slim(paper: dict) -> dict:
    abstract = str(paper.get("abstract") or "")
    return {
        "id": paper.get("id", ""),
        "title": paper.get("title", ""),
        "authors": paper.get("authors", []),
        "year": paper.get("year"),
        "venue": paper.get("venue", ""),
        "doi": paper.get("doi", ""),
        "source": paper.get("source", ""),
        "citation_count": paper.get("citation_count"),
        "abstract": abstract[:_ABSTRACT_CAP] + ("…" if len(abstract) > _ABSTRACT_CAP else ""),
        "keywords": paper.get("keywords", []),
        "urls": paper.get("urls", []),
    }


def collect_papers(user_id: str) -> list[dict]:
    """Aggregate network-paper metadata from one owner's sessions, newest session first."""
    from core.history_store import HISTORY_DIR, _owner_of

    merged: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    try:
        files = sorted(HISTORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _owner_of(data) != user_id:
            continue
        session_ref = {"filename": fp.name, "title": data.get("title") or data.get("topic") or ""}
        # papers(已选入综述)优先于 candidates(候选池),先写不被覆盖。
        for field, origin in (("candidates", "candidate"), ("papers", "paper")):
            for paper in data.get(field) or []:
                if not isinstance(paper, dict) or not str(paper.get("title") or "").strip():
                    continue
                key = _dedup_key(paper)
                entry = merged.get(key)
                if entry is None:
                    entry = merged[key] = {"paper": _slim(paper), "sessions": [],
                                           "origin": origin}
                    order.append(key)
                if entry["origin"] != "paper" and origin == "paper":
                    entry["paper"] = _slim(paper)
                    entry["origin"] = "paper"
                if session_ref not in entry["sessions"]:
                    entry["sessions"].append(session_ref)
    out: list[dict] = []
    for key in order[:_MAX_PAPERS]:
        entry = merged[key]
        out.append({**entry["paper"], "sessions": entry["sessions"][:5],
                    "in_review": entry["origin"] == "paper"})
    return out


@router.get("")
def list_papers(authorization: str | None = Header(None),
                x_guest_id: str | None = Header(None)):
    user = current_user(authorization, x_guest_id)
    return {"papers": collect_papers(user["id"])}
