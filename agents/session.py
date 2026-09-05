"""Chat session state + persistence (unified history store).

A ChatSession is everything one conversation accumulates: messages, the
paper set (core + candidates), structured summaries, the research map, the
literature review, and RAG scope (session_id). Cross-session there is NO
long-term memory by design — the SQLite summary cache only saves LLM cost,
it is never exposed as memory.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from core.models import Paper, PaperSummary, summaries_from_dicts
from core.storage_context import StorageContext


@dataclass
class ChatSession:
    """Accumulated state of one conversation."""

    messages: list[dict] = field(default_factory=list)
    topic: str = ""
    conception: str = ""
    language: str = "both"
    field_profile: str = "general"
    papers: list[Paper] = field(default_factory=list)        # 核心集
    candidates: list[Paper] = field(default_factory=list)    # 候选集其余
    paper_summaries: dict[str, PaperSummary] = field(default_factory=dict)
    map_data: dict = field(default_factory=dict)             # research_map 产物
    reading_path: list[dict] = field(default_factory=list)   # reading_path 产物
    literature_review: str = ""
    review_metadata: dict = field(default_factory=dict)
    sub_directions: list = field(default_factory=list)       # 意图理解产物
    search_queries: list = field(default_factory=list)
    history_summary: str = ""                                # 旧消息压缩摘要
    attachments: list[dict] = field(default_factory=list)
    title: str = ""
    history_filename: str | None = None
    trace_ids: list[str] = field(default_factory=list)
    session_id: str = ""                                     # RAG 隔离键
    loaded_skills: set[str] = field(default_factory=set)     # 本会话已加载的指令技能（去重，省 token；不持久化）
    channel: str = "web"                                    # Storage/lifecycle channel; web remains the default.
    owner_id: str = ""                                      # Web artifact owner; never persisted as public content.
    storage_context: StorageContext | None = field(default=None, repr=False, compare=False)
    checkpoint_version: int | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.channel not in {"web", "openai_api"}:
            raise ValueError(f"unsupported session channel: {self.channel}")
        if self.storage_context is not None and self.storage_context.channel != self.channel:
            raise ValueError("session channel and storage context must match")
        if not self.session_id:
            self.session_id = uuid.uuid4().hex[:16]

    # -- state probes ------------------------------------------------------

    def has_papers(self) -> bool:
        return len(self.papers) > 0

    def has_map(self) -> bool:
        return bool(self.map_data)

    def has_review(self) -> bool:
        return bool(self.literature_review)

    def all_papers(self) -> list[Paper]:
        return list(self.papers) + list(self.candidates)

    @staticmethod
    def _normalized_doi_alias(value: str | None) -> str:
        """Return a canonical DOI alias, or ``""`` for non-DOI identifiers."""
        value = (value or "").strip().casefold()
        for prefix in (
            "https://doi.org/", "http://doi.org/",
            "https://dx.doi.org/", "http://dx.doi.org/", "doi:",
        ):
            if value.startswith(prefix):
                value = value[len(prefix):]
                break
        value = value.rstrip("/")
        # DOI syntax starts with 10.<registrant>/<suffix>. Do not loosen hash,
        # arXiv, upload or other stable ids into case-insensitive aliases.
        return value if value.startswith("10.") and "/" in value else ""

    def paper_by_id(self, paper_id: str) -> Paper | None:
        papers = self.all_papers()
        # Stable ids are authoritative and avoid ambiguity.
        for paper in papers:
            if paper.id == paper_id:
                return paper

        wanted = self._normalized_doi_alias(paper_id)
        if not wanted:
            return None
        matches = []
        for paper in papers:
            aliases = {
                self._normalized_doi_alias(paper.id),
                self._normalized_doi_alias(paper.doi),
            }
            if wanted in aliases:
                matches.append(paper)
        # Never guess across duplicate/ambiguous aliases.
        return matches[0] if len(matches) == 1 else None

    def paper_by_title(self, title: str) -> Paper | None:
        """Exact (whitespace/case-insensitive) title lookup across all papers.

        Used as a guard against the model re-running search_papers just to
        locate a paper the session already owns. Ambiguous duplicates return
        None so the caller rejects rather than guesses.
        """
        wanted = " ".join((title or "").split()).casefold()
        if not wanted:
            return None
        matches = [p for p in self.all_papers()
                   if " ".join((p.title or "").split()).casefold() == wanted]
        return matches[0] if len(matches) == 1 else None

    def context_summary(self) -> str:
        """Compact session catalogue for the model.

        Network papers are addressable for metadata/abstract questions only;
        uploaded attachments are the only full-document entry point.
        """
        parts = []
        if self.topic:
            parts.append(f"主题: {self.topic}")
        if self.papers or self.candidates:
            parts.append(f"核心集 {len(self.papers)} 篇 / 候选 {len(self.candidates)} 篇")
            catalog: list[str] = []
            if self.papers:
                catalog.append(
                    "核心集 paper_ids（可直接用于 ask_papers/citation_export；"
                    "不要为这些论文再次 search_papers；网络论文只提供摘要证据）："
                )
                catalog.extend(
                    f"- {p.id} | {p.title}" for p in self.papers if p.id
                )
            if self.candidates:
                catalog.append(
                    "候选集 paper_ids（同样可用于摘要级 ask_papers；"
                    "禁止再次 search_papers）："
                )
                catalog.extend(
                    f"- {p.id} | {p.title}" for p in self.candidates[:25] if p.id
                )
                if len(self.candidates) > 25:
                    catalog.append(f"... 其余 {len(self.candidates) - 25} 篇候选未列出")
            parts.append("\n".join(catalog))
        if self.paper_summaries:
            parts.append(f"已深读 {len(self.paper_summaries)} 篇")
            if self.channel == "openai_api":
                read_titles = [p.title for p in self.all_papers()
                               if p.id in self.paper_summaries and p.title][:5]
                if read_titles:
                    parts.append("已深读论文: " + "；".join(read_titles))
        if self.map_data:
            parts.append(f"研究地图已生成（{len(self.map_data.get('clusters', []))} 个主题簇）")
        if self.reading_path:
            parts.append(f"阅读路径 {len(self.reading_path)} 篇")
        if self.literature_review:
            parts.append(f"综述已生成（{len(self.literature_review)} 字符）")
        return "\n".join(parts) if parts else "尚未调用任何工具。"


# ---------------------------------------------------------------------------
# Persistence (thin wrappers over core.history_store)
# ---------------------------------------------------------------------------

def _summary_to_dict(s) -> dict:
    if isinstance(s, dict):
        return s
    if hasattr(s, "to_dict"):
        return s.to_dict()
    return {"paper_id": getattr(s, "paper_id", "")}


def save_chat_history(session: ChatSession, filepath: str | None = None,
                      user_id: str = "local") -> str:
    """Persist the session; returns the bare history filename.
    `user_id` stamps the record owner (multi-user isolation)."""
    from core.history_store import save_session

    data = {
        "type": "chat",
        "topic": session.topic,
        "conception": session.conception,
        "language": session.language,
        "field_profile": session.field_profile,
        "messages": session.messages,
        "papers": [p.to_dict() for p in session.papers],
        "candidates": [p.to_dict() for p in session.candidates],
        "paper_summaries": {pid: _summary_to_dict(s) for pid, s in session.paper_summaries.items()},
        "map_data": session.map_data,
        "reading_path": session.reading_path,
        "literature_review": session.literature_review,
        "review_metadata": session.review_metadata,
        "sub_directions": session.sub_directions,
        "search_queries": session.search_queries,
        "history_summary": session.history_summary,
        "attachments": session.attachments,
        "title": session.title,
        "history_filename": session.history_filename,
        "trace_ids": list(session.trace_ids),
        "session_id": session.session_id,
    }
    fname = save_session(data, filename=filepath or session.history_filename,
                         user_id=user_id)
    session.history_filename = fname
    return fname


def load_chat_history(filepath: str, user_id: str | None = None) -> ChatSession | None:
    """Load a session record. Unknown/legacy fields are ignored safely.
    With `user_id`, another owner's record loads as not-found (None).

    Legacy compat: old records stored reserve_papers / reference_papers
    (two extra tiers) — they are merged into `candidates`.
    """
    from core.history_store import load_session

    data = load_session(filepath, user_id=user_id)
    if data is None:
        return None

    candidates_raw = list(data.get("candidates", []))
    if not candidates_raw:  # legacy record
        candidates_raw = list(data.get("reserve_papers", [])) + list(data.get("reference_papers", []))

    return ChatSession(
        messages=data.get("messages", []),
        topic=data.get("topic", ""),
        conception=data.get("conception", ""),
        language=data.get("language", "both"),
        field_profile=data.get("field_profile", "general"),
        papers=[Paper.from_dict(p) for p in data.get("papers", [])],
        candidates=[Paper.from_dict(p) for p in candidates_raw],
        paper_summaries=summaries_from_dicts(data.get("paper_summaries", {})),
        map_data=data.get("map_data", {}),
        reading_path=data.get("reading_path", []),
        literature_review=data.get("literature_review", ""),
        review_metadata=data.get("review_metadata", {}),
        sub_directions=data.get("sub_directions", data.get("subtopics", [])),
        search_queries=data.get("search_queries", []),
        history_summary=data.get("history_summary", ""),
        attachments=data.get("attachments", []),
        title=data.get("title", ""),
        history_filename=filepath if isinstance(filepath, str) else data.get("history_filename"),
        trace_ids=list(data.get("trace_ids", [])),
        session_id=data.get("session_id", ""),
        owner_id=user_id or "",
    )


def list_chat_history(user_id: str | None = None) -> list[dict]:
    from core.history_store import list_sessions
    return list_sessions(user_id=user_id)


def rename_chat_history(filename: str, new_title: str,
                        user_id: str | None = None) -> bool:
    from core.history_store import rename_session
    return rename_session(filename, new_title, user_id=user_id) is not None


def delete_chat_history(filename: str, user_id: str | None = None) -> bool:
    """Delete a session record and its session-scoped RAG vectors."""
    from core.history_store import delete_session, load_session

    data = load_session(filename, user_id=user_id)
    ok = delete_session(filename, user_id=user_id)
    if ok and data and data.get("session_id"):
        from core.reading_store import delete_session as delete_reading_session
        delete_reading_session(str(data.get("user_id") or "local"), data["session_id"])
        try:
            from tools.storage.vectorstore import VectorStore
            VectorStore().delete_session(data["session_id"])
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
    return ok
