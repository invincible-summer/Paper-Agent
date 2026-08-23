"""Evidence-bounded, staged literature-review writer.

Network records are deliberately an abstract-only evidence source.  A user
uploaded document is the only source that can contribute full-text claims.
The module keeps the evidence preparation and citation rendering deterministic
so an unavailable auxiliary model degrades to a useful, auditable review.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from core.blocking import run_io_bound
from core.config import get_settings
from core.llm import ainvoke_utility, get_llm
from core.models import Paper
from core.paper_search_settings_store import paper_abstract_text
from core.prompts import review_prompts as _review_prompts  # noqa: F401 - register prompts
from core.prompts.registry import get as get_prompt
from core.state import ResearchState

logger = logging.getLogger(__name__)

_REVIEW_HEADINGS = (
    "# 引言",
    "# 综述范围与证据基础",
    "# 分类框架与分主题综述",
    "# 方法、发现与发展脉络的综合比较",
    "# 主要争议、共同局限与研究空白",
    "# 未来研究方向",
    "# 结论",
    "# 纳入文献与证据层级说明",
)

# Explicit auxiliary-generation ceilings.  They are intentionally separate
# from the administrator's tool wall-clock budget: the latter controls the
# whole write_review call, while these limits bound each staged completion.
REVIEW_GENERATION_BUDGETS = {
    "review.upload_chunk": 650,
    "review.outline": 900,
    "review.theme": 2200,
    "review.synthesis": 2600,
    "review.revision": 5200,
    "review.expansion": 3200,
}


def _report(callback, message: str) -> None:
    if callback:
        callback(message)


def _obj_text(value: Any) -> str:
    """Extract text from a LangChain response without assuming its shape."""
    content = getattr(value, "content", value)
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "").strip()


def _paper_id_map(papers: list[Paper]) -> dict[str, Paper]:
    return {str(p.id): p for p in papers if getattr(p, "id", "")}


def _read_attachment_text(attachment: dict, state: ResearchState) -> str:
    """Read the complete sidecar; never replace it with a head preview."""
    supplied = state.get("attachment_texts", {}) or {}
    aid = str(attachment.get("id") or "")
    if isinstance(supplied, dict) and aid in supplied:
        return str(supplied[aid] or "")
    session = state.get("session")
    if session is None:
        return ""
    try:
        from tools.ingest.attachments import attachment_text_path
        path = attachment_text_path(attachment, session)
        return path.read_text(encoding="utf-8") if path and path.is_file() else ""
    except (OSError, TypeError, ValueError):
        return ""


def _attachment_matches(attachments: list[dict], requested: list[str]) -> tuple[list[dict], list[str]]:
    if not requested:
        return attachments, []
    selected: list[dict] = []
    missing: list[str] = []
    seen: set[str] = set()
    for ref in requested:
        ref = str(ref or "").strip()
        found = next((a for a in attachments if str(a.get("id") or "") == ref), None)
        if found is None:
            # Filename acceptance is exact and unambiguous, matching the
            # attachment lookup boundary used by the other upload tools.
            matches = [a for a in attachments if str(a.get("filename") or "").casefold() == ref.casefold()]
            found = matches[0] if len(matches) == 1 else None
        if found is None:
            missing.append(ref)
            continue
        aid = str(found.get("id") or "")
        if aid and aid not in seen:
            selected.append(found)
            seen.add(aid)
    return selected, missing


def _chunk_upload(aid: str, text: str, state: ResearchState) -> tuple[list[dict], str]:
    """Build complete parent/child chunks and keep a fingerprinted cache."""
    from tools.storage.vectorstore import build_fulltext_chunks

    fingerprint = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    cache = state.setdefault("review_chunk_cache", {})
    key = f"upload:{aid}:{fingerprint}"
    cached = cache.get(key)
    if isinstance(cached, list) and cached:
        return cached, fingerprint
    chunks = build_fulltext_chunks(None, text)
    if not chunks:
        chunks = [{"section": "full", "text": text, "parent_text": text, "parent_id": "full::p0"}]
    # The cache contains all chunks, including tail sections.  This is not a
    # prompt preview and is intentionally never clipped.
    cache[key] = chunks
    return chunks, fingerprint


def _chunk_digest_fallback(item: dict[str, Any], chunk: dict[str, Any], index: int) -> str:
    """Return an evidence-preserving digest when the auxiliary model is down.

    Parent chunks are deliberately small (about 1500 characters), so keeping
    the complete parent text here ensures a transient LLM failure cannot make
    a middle or tail section disappear from a review.
    """
    body = str(chunk.get("parent_text") or chunk.get("text") or "").strip()
    section = str(chunk.get("section") or "正文")
    return f"第 {index + 1} 个分块（{section}）的可核查原文：{body}"


def _load_upload_digest_cache(item: dict[str, Any], state: ResearchState) -> dict[str, str]:
    """Load one upload's private, fingerprinted review digest cache."""
    session = state.get("session")
    if session is None or state.get("attachment_texts"):
        return {}
    from tools.storage.database import Database
    context = getattr(session, "storage_context", None)
    db = Database(storage_context=context) if context is not None else Database(get_settings().storage.sqlite_path)
    try:
        raw = db.get_cached_summary(
            str(item["id"]), f"review-v1:{item.get('doc_fingerprint') or ''}", "review_upload",
        )
        data = json.loads(raw) if raw else {}
        digests = data.get("digests") if isinstance(data, dict) else None
        return {str(key): str(value) for key, value in (digests or {}).items() if str(value).strip()}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    finally:
        db.close()


def _save_upload_digest_cache(item: dict[str, Any], state: ResearchState, digests: dict[str, str]) -> None:
    """Persist only upload-derived structured summaries, never network text."""
    session = state.get("session")
    if session is None or state.get("attachment_texts") or not digests:
        return
    from tools.storage.database import Database
    context = getattr(session, "storage_context", None)
    db = Database(storage_context=context) if context is not None else Database(get_settings().storage.sqlite_path)
    try:
        payload = json.dumps({
            "version": 1, "fingerprint": item.get("doc_fingerprint") or "",
            "digests": digests,
        }, ensure_ascii=False, sort_keys=True)
        db.save_cached_summary(
            str(item["id"]), f"review-v1:{item.get('doc_fingerprint') or ''}",
            "review_upload", payload,
        )
    finally:
        db.close()


async def _prepare_upload_digests(
    evidence: list[dict[str, Any]], state: ResearchState, progress_callback=None,
) -> None:
    """Map-reduce every unique uploaded parent chunk into a private digest.

    The raw complete sidecar remains in ``chunks``. The structured digest is
    the bounded-generation representation used by later outline/theme stages;
    its cache key includes the upload fingerprint and therefore cannot be
    reused for a changed file or a network paper.
    """
    uploads = [item for item in evidence if item.get("kind") == "upload"]
    if not uploads:
        return
    cache = state.setdefault("review_chunk_digest_cache", {})
    session = state.get("session")
    if session is not None:
        metadata = getattr(session, "review_metadata", None)
        if isinstance(metadata, dict):
            persisted = metadata.setdefault("_upload_chunk_digests", {})
            if isinstance(persisted, dict):
                for key, value in persisted.items():
                    cache.setdefault(str(key), str(value))

    persisted_by_upload = await asyncio.gather(*(
        run_io_bound(_load_upload_digest_cache, item, state) for item in uploads
    ))
    for item, persisted_digests in zip(uploads, persisted_by_upload):
        for parent_id, digest in persisted_digests.items():
            key = f"upload:{item.get('attachment_id')}:{item.get('doc_fingerprint') or ''}:{parent_id}"
            cache.setdefault(key, digest)

    jobs: list[tuple[dict[str, Any], str, int, dict[str, Any]]] = []
    for item in uploads:
        seen_parents: set[str] = set()
        for index, chunk in enumerate(item.get("chunks") or []):
            parent_id = str(chunk.get("parent_id") or f"chunk-{index}")
            if parent_id in seen_parents:
                continue
            seen_parents.add(parent_id)
            jobs.append((item, parent_id, index, chunk))

    semaphore = asyncio.Semaphore(4)

    async def one(item: dict[str, Any], parent_id: str, index: int, chunk: dict[str, Any]) -> tuple[str, str]:
        fingerprint = str(item.get("doc_fingerprint") or "")
        key = f"upload:{item.get('attachment_id')}:{fingerprint}:{parent_id}"
        cached = cache.get(key)
        if isinstance(cached, str) and cached.strip():
            return key, cached
        async with semaphore:
            prompt_text = str(chunk.get("parent_text") or chunk.get("text") or "").strip()
            digest = await _call_prompt(
                "review.upload_chunk",
                evidence_id=item["id"], filename=item.get("filename") or item["id"],
                chunk_index=index + 1, section=chunk.get("section") or "正文",
                chunk_text=prompt_text,
            )
        digest = digest.strip() or _chunk_digest_fallback(item, chunk, index)
        cache[key] = digest
        return key, digest

    results = await asyncio.gather(*(one(*job) for job in jobs))
    persisted = None
    if session is not None and isinstance(getattr(session, "review_metadata", None), dict):
        persisted = session.review_metadata.setdefault("_upload_chunk_digests", {})
    for key, digest in results:
        cache[key] = digest
        if isinstance(persisted, dict):
            persisted[key] = digest

    by_upload: dict[str, list[tuple[int, str, str]]] = {}
    for item, parent_id, index, chunk in jobs:
        key = f"upload:{item.get('attachment_id')}:{item.get('doc_fingerprint') or ''}:{parent_id}"
        digest = cache.get(key) or _chunk_digest_fallback(item, chunk, index)
        by_upload.setdefault(str(item["id"]), []).append(
            (index, str(chunk.get("section") or "正文"), digest)
        )
    for item in uploads:
        rows = sorted(by_upload.get(str(item["id"]), []), key=lambda row: row[0])
        item["structured_digest"] = "\n\n".join(
            f"[分块 {index + 1}｜{section}] {digest}" for index, section, digest in rows
        )
    save_jobs = []
    for item in uploads:
        prefix = f"upload:{item.get('attachment_id')}:{item.get('doc_fingerprint') or ''}:"
        digests = {key[len(prefix):]: value for key, value in cache.items() if key.startswith(prefix)}
        save_jobs.append(run_io_bound(_save_upload_digest_cache, item, state, digests))
    await asyncio.gather(*save_jobs)
    _report(progress_callback, f"上传全文分块归纳完成：{len(jobs)} 个独立分块已覆盖并缓存。")


def prepare_review_evidence(state: ResearchState) -> dict[str, Any]:
    """Collect the sole evidence boundary used by every review stage.

    Returns ``included`` records plus explicit ``excluded`` reasons.  The
    function is synchronous and deterministic, making it straightforward to
    test without credentials or a live search service.
    """
    papers = list(state.get("papers") or []) + list(state.get("candidate_papers") or [])
    if not state.get("candidate_papers"):
        papers += list(state.get("candidates") or [])
    # De-duplicate by stable id while preserving core-before-candidate order.
    unique: list[Paper] = []
    seen: set[str] = set()
    for paper in papers:
        pid = str(getattr(paper, "id", "") or "")
        if pid and pid not in seen:
            unique.append(paper)
            seen.add(pid)

    requested_ids = [str(x) for x in (state.get("paper_ids") or []) if str(x).strip()]
    if requested_ids:
        requested_set = set(requested_ids)
        # Exact IDs are authoritative here.  The caller resolves DOI aliases
        # before invoking the agent; silently guessing would violate scope.
        selected_papers = [p for p in unique if p.id in requested_set]
        missing_ids = [x for x in requested_ids if x not in {p.id for p in selected_papers}]
    else:
        selected_papers, missing_ids = unique, []

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = [
        {"id": pid, "kind": "network", "reason": "not_in_current_session"}
        for pid in missing_ids
    ]
    for paper in selected_papers:
        abstract = paper_abstract_text(paper).strip()
        if not abstract:
            excluded.append({
                "id": paper.id, "kind": "network", "title": paper.title,
                "reason": "missing_or_disabled_abstract",
            })
            continue
        included.append({
            "id": paper.id,
            "kind": "network",
            "evidence_level": "abstract",
            "title": paper.title or paper.id,
            "abstract": abstract,
            "source_text": abstract,
            "landing_url": _paper_link(paper),
            "year": paper.year,
            "authors": list(paper.authors or []),
            "paper": paper,
            "chunks": [{"section": "abstract", "text": abstract, "parent_text": abstract}],
        })

    attachments = list(state.get("attachments") or [])
    selected_attachments, missing_attachments = _attachment_matches(
        attachments, [str(x) for x in (state.get("attachment_ids") or [])]
    )
    excluded.extend({"id": aid, "kind": "upload", "reason": "not_in_current_session"}
                    for aid in missing_attachments)
    for attachment in selected_attachments:
        aid = str(attachment.get("id") or "")
        text = _read_attachment_text(attachment, state)
        if not aid or not text.strip():
            excluded.append({
                "id": aid, "kind": "upload", "filename": attachment.get("filename", aid),
                "reason": "unparseable_or_empty_sidecar",
            })
            continue
        chunks, fingerprint = _chunk_upload(aid, text, state)
        included.append({
            "id": f"upload:{aid}",
            "attachment_id": aid,
            "kind": "upload",
            "evidence_level": "uploaded_fulltext",
            "title": attachment.get("filename") or aid,
            "filename": attachment.get("filename") or aid,
            "source_text": text,  # complete text, not a head excerpt
            "char_count": len(text),
            "doc_fingerprint": fingerprint,
            "chunks": chunks,
            "landing_url": None,
        })
    return {
        "included": included,
        "excluded": excluded,
        "included_count": len(included),
        "excluded_count": len(excluded),
        "network_abstract_count": sum(x["kind"] == "network" for x in included),
        "uploaded_fulltext_count": sum(x["kind"] == "upload" for x in included),
    }


def _prompt_evidence(evidence: list[dict[str, Any]], max_chars: int = 120_000) -> str:
    """Format all evidence, using a complete per-chunk upload digest.

    Unlike the old head-preview path, no upload chunk is dropped from the
    structured representation. ``max_chars`` is retained for compatibility
    and only applies to an unexpected raw-text fallback.
    """
    blocks: list[str] = []
    for item in evidence:
        if item["kind"] == "network":
            body = item["abstract"]
        else:
            body = str(item.get("structured_digest") or "").strip()
            if not body:
                chunks = item.get("chunks") or []
                body = "\n\n".join(
                    f"[分块 {i + 1}｜{c.get('section') or '正文'}] "
                    f"{c.get('parent_text') or c.get('text') or ''}"
                    for i, c in enumerate(chunks)
                )
                if len(body) > max_chars:
                    half = max_chars // 2
                    body = body[:half] + "\n...[原文分块缓存仍完整保留]...\n" + body[-half:]
        blocks.append(
            f"[{item['id']}] level={item['evidence_level']} title={item['title']}\n{body}"
        )
    return "\n\n---\n\n".join(blocks) or "（没有有效证据）"


def _minimum_target(evidence_count: int, upload_count: int, requested: int, focus: str) -> tuple[int, str]:
    concise = any(word in (focus or "") for word in ("简要", "简短", "概述", "摘要式"))
    if concise:
        minimum = 1200
    elif evidence_count <= 2 and upload_count == 0:
        minimum = 1800
    else:
        minimum = min(6000, max(3000, 700 * evidence_count + 500 * upload_count))
    target = max(minimum, int(requested or 0))
    target = min(target, 12_000)
    note = "按材料覆盖需要自动提高篇幅" if requested and requested < minimum else "按用户期望篇幅生成"
    if not requested:
        note = "按有效材料数量自适应篇幅"
    if concise:
        note = "用户要求简要概述，采用短版但保留证据边界"
    return target, note


def _fallback_themes(evidence: list[dict[str, Any]], graph_data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    graph_data = graph_data or {}
    raw = graph_data.get("clusters") or {}
    if isinstance(raw, list):
        raw = {str(c.get("id", i)): c for i, c in enumerate(raw) if isinstance(c, dict)}
    valid = {x["id"] for x in evidence}
    themes: list[dict[str, Any]] = []
    for cid, cluster in raw.items() if isinstance(raw, dict) else []:
        ids = [str(x) for x in (cluster.get("paper_ids") or []) if str(x) in valid]
        if ids:
            themes.append({"id": f"theme-{cid}", "title": cluster.get("label") or f"研究主题 {cid}",
                           "question": "比较该主题的研究路径、发现与局限", "evidence_ids": ids})
    if themes:
        assigned = {i for t in themes for i in t["evidence_ids"]}
        leftovers = [x["id"] for x in evidence if x["id"] not in assigned]
        if leftovers:
            themes[0]["evidence_ids"].extend(leftovers)
        return themes[:6]
    ids = [x["id"] for x in evidence]
    if len(ids) <= 2:
        return [{"id": "theme-1", "title": "研究问题与方法路径", "question": "比较问题界定与方法选择", "evidence_ids": ids}]
    split = max(1, len(ids) // 2)
    return [
        {"id": "theme-1", "title": "研究问题与方法路径", "question": "比较问题界定与方法选择", "evidence_ids": ids[:split]},
        {"id": "theme-2", "title": "主要发现、演进与局限", "question": "比较发现、发展关系和限制", "evidence_ids": ids[split:]},
    ]


def _parse_outline(raw: str, evidence: list[dict[str, Any]], graph_data: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
        themes = data.get("themes") if isinstance(data, dict) else None
        valid = {x["id"] for x in evidence}
        out = []
        for i, theme in enumerate(themes or []):
            if not isinstance(theme, dict):
                continue
            ids = [str(x) for x in theme.get("evidence_ids", []) if str(x) in valid]
            if ids:
                out.append({"id": str(theme.get("id") or f"theme-{i+1}"),
                            "title": str(theme.get("title") or f"研究主题 {i+1}"),
                            "question": str(theme.get("question") or "比较方法、发现、演进与局限"),
                            "evidence_ids": ids})
        if out:
            assigned = {i for t in out for i in t["evidence_ids"]}
            leftovers = [x["id"] for x in evidence if x["id"] not in assigned]
            if leftovers:
                out[0]["evidence_ids"].extend(leftovers)
            return out[:6]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return _fallback_themes(evidence, graph_data)


def _deterministic_theme(theme: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    lines = [f"{theme['title']}围绕“{theme.get('question', '方法、发现与局限')}”展开综合比较。"]
    items = [by_id[i] for i in theme.get("evidence_ids", []) if i in by_id]
    for item in items:
        source = item.get("structured_digest") if item.get("kind") == "upload" else item.get("source_text")
        snippet = " ".join(str(source or item.get("source_text", "")).split())
        if len(snippet) > 420:
            # Keep both the opening problem/method and the tail where papers
            # commonly report limitations and future work.
            snippet = snippet[:210] + "……[中段省略]……" + snippet[-210:]
        level = "摘要级" if item["kind"] == "network" else "上传全文级"
        lines.append(f"代表工作“{item['title']}”（{level}）显示：{snippet} [{item['id']}]")
    if len(items) > 1:
        lines.append("这些工作在问题界定和方法路径上存在差异，现有材料支持比较其研究取向，但不能据此补写摘要未报告的精确实验细节。")
    lines.append("共同局限是证据范围与报告粒度不完全一致；网络论文的细节仍需上传原文核验。")
    return "\n\n".join(lines)


def _deterministic_synthesis(themes: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]) -> dict[str, str]:
    names = "、".join(t["title"] for t in themes) or "现有主题"
    ids = [i for t in themes for i in t.get("evidence_ids", [])]
    citations = "、".join(f"[{i}]" for i in ids[:8])
    return {
        "comparison": f"综合来看，{names}分别从不同层面处理研究问题。材料显示，方法选择与研究目标之间存在对应关系；上传全文提供了更细的论证线索，而网络论文只能支撑有效摘要明确报告的判断。代表证据包括 {citations}。",
        "gaps": f"主要争议和共同局限集中在方法口径、评价范围及证据粒度差异上。不同主题之间尚缺少可直接对照的统一设计，这一判断基于当前纳入材料，不能替代全文核验。相关证据为 {citations}。",
        "future": "未来研究可优先建立可复现的比较框架，统一数据、评价指标和报告规范，并对摘要中无法确认的实验细节通过上传原文进行核验。",
    }


async def _call_prompt(prompt_id: str, **kwargs: Any) -> str:
    prompt = get_prompt(prompt_id).text.format(**kwargs)
    llm = get_llm("light")
    budget = REVIEW_GENERATION_BUDGETS.get(prompt_id, 1800)
    try:
        response = await ainvoke_utility(llm, [HumanMessage(content=prompt)], max_tokens=budget)
    except Exception as first_error:  # test doubles and older endpoints
        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)], max_tokens=budget)
        except Exception:
            try:
                response = await llm.ainvoke([HumanMessage(content=prompt)])
            except Exception:
                logger.debug("review prompt %s failed: %s", prompt_id, first_error)
                return ""
    return _obj_text(response)


def _validate_citations(text: str, valid_ids: set[str], excluded_ids: set[str] | None = None) -> str:
    """Replace every unknown evidence token, while preserving Markdown links."""
    excluded_ids = excluded_ids or set()
    pattern = re.compile(r"\[([^]\n]+)\](?!\()")

    def replace(match: re.Match) -> str:
        token = match.group(1).strip()
        if token in valid_ids:
            return f"[{token}]"
        if token in {"Link", "链接", "CITATION NEEDED", "证据不足"}:
            return match.group(0)
        if token in excluded_ids or token.startswith(("doi:", "hash:", "upload:")):
            return "[证据不足]"
        return "[证据不足]"

    return pattern.sub(replace, text or "")


def _paper_link(p: Paper) -> str | None:
    """Return a landing page only; never expose a remote PDF URL."""
    if p.urls:
        for src in ("doi", "openalex", "arxiv", "crossref", "europepmc", "doaj", "semantic_scholar", "pubmed"):
            url = p.urls.get(src)
            if isinstance(url, str) and url.startswith(("http://", "https://")) and not url.lower().endswith(".pdf"):
                return url
        for url in p.urls.values():
            if isinstance(url, str) and url.startswith(("http://", "https://")) and ".pdf" not in url.lower():
                return url
    if p.doi:
        return f"https://doi.org/{p.doi.lstrip('/')}"
    return None


def _render_evidence_citations(text: str, evidence_map: dict[str, dict[str, Any]]) -> str:
    ids = sorted((i for i in evidence_map if i), key=len, reverse=True)
    if not ids:
        return text
    pattern = re.compile(r"\[(" + "|".join(re.escape(i) for i in ids) + r")\](?!\()")

    def repl(match: re.Match) -> str:
        item = evidence_map[match.group(1)]
        label = (item.get("filename") if item.get("kind") == "upload" else item.get("title")) or match.group(1)
        if item.get("kind") == "upload":
            return str(label)
        link = item.get("landing_url")
        return f"{label} ([Link]({link}))" if link else str(label)

    return pattern.sub(repl, text)


def _render_citations_as_titles(text: str, paper_map: dict[str, Paper]) -> str:
    """Compatibility helper for network-paper-only callers/tests."""
    evidence = {
        pid: {"kind": "network", "title": p.title or pid, "landing_url": _paper_link(p)}
        for pid, p in paper_map.items()
    }
    return _render_evidence_citations(text, evidence)


def _build_papers_list(papers: list[Paper], summaries: dict | None = None) -> str:
    """Legacy prompt helper, now full valid abstracts rather than 300 chars."""
    lines = []
    for paper in papers:
        abstract = paper_abstract_text(paper).strip()
        if abstract:
            lines.append(f"[{paper.id}] {paper.title}（{paper.year or 'n.d.'}）\n摘要：{abstract}")
    return "\n\n".join(lines)


def _split_synthesis(text: str, fallback: dict[str, str]) -> dict[str, str]:
    if not text.strip():
        return fallback
    # Accept either headings from a capable model or use the whole response as
    # comparison; the fixed outer Markdown structure remains deterministic.
    chunks = re.split(r"^#{1,3}\s*", text, flags=re.MULTILINE)
    result = {"comparison": "", "gaps": "", "future": ""}
    for chunk in chunks:
        low = chunk.casefold()
        body = chunk.split("\n", 1)[1].strip() if "\n" in chunk else chunk.strip()
        if any(x in low for x in ("争议", "局限", "空白", "gap")):
            result["gaps"] += ("\n\n" if result["gaps"] else "") + body
        elif any(x in low for x in ("未来", "方向", "future")):
            result["future"] += ("\n\n" if result["future"] else "") + body
        elif body:
            result["comparison"] += ("\n\n" if result["comparison"] else "") + body
    for key, value in fallback.items():
        if not result[key].strip():
            result[key] = value
    return result


def _ensure_headings(review: str, fallback: str) -> str:
    return review if all(h in review for h in _REVIEW_HEADINGS) else fallback


def _empty_review(topic: str, excluded_count: int) -> str:
    sections = {
        "# 引言": f"主题“{topic}”目前没有可用于综述的有效证据，因此不生成基于标题或模型常识的内容。",
        "# 综述范围与证据基础": (
            f"本次纳入 0 条证据，跳过 {excluded_count} 条。网络论文必须具有当前启用且非空的摘要；"
            "全文级分析只能来自用户上传并可解析的论文文件。"
        ),
        "# 分类框架与分主题综述": "证据不足，无法建立有依据的分类框架。",
        "# 方法、发现与发展脉络的综合比较": "证据不足，无法比较方法、发现或发展关系。",
        "# 主要争议、共同局限与研究空白": "现阶段唯一可确认的限制是缺少合格证据，不能据此推断领域争议或研究空白。",
        "# 未来研究方向": "请获取有效摘要，或上传 PDF、DOCX、TXT、MD、TEX 论文文件后重新生成。",
        "# 结论": "由于没有纳入证据，本次不形成实质性综述结论。",
        "# 纳入文献与证据层级说明": "未纳入任何文献；没有生成或保留任何文献引用。",
    }
    return "\n\n".join(f"{heading}\n\n{body}" for heading, body in sections.items()) + "\n"


def _review_content_length(text: str) -> int:
    body = re.sub(r"^#{1,6}\s+.*$", "", text or "", flags=re.MULTILINE)
    body = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", body)
    return len(re.sub(r"\s+", "", body))


async def review_agent(state: ResearchState, progress_callback=None) -> ResearchState:
    """Run evidence preparation → outline → parallel themes → synthesis → revision."""
    topic = str(state.get("topic") or "未命名主题")
    focus = str(state.get("focus") or "").strip()
    language = str(state.get("language") or "both")
    evidence_info = prepare_review_evidence(state)
    evidence = evidence_info["included"]
    by_id = {item["id"]: item for item in evidence}
    excluded_ids = {str(item.get("id")) for item in evidence_info["excluded"] if item.get("id")}
    target, length_note = _minimum_target(
        len(evidence), evidence_info["uploaded_fulltext_count"], int(state.get("target_length") or 0), focus
    )
    state["review_evidence"] = [
        {k: v for k, v in item.items() if k not in {"paper", "source_text", "chunks", "structured_digest"}}
        for item in evidence
    ]
    state["review_exclusions"] = evidence_info["excluded"]
    state["review_stats"] = {
        **{k: evidence_info[k] for k in ("included_count", "excluded_count", "network_abstract_count", "uploaded_fulltext_count")},
        "target_length": target, "length_note": length_note,
    }
    if not evidence:
        state["literature_review"] = _empty_review(topic, len(evidence_info["excluded"]))
        state["review_stats"]["actual_length"] = _review_content_length(state["literature_review"])
        state["review_actual_target_length"] = target
        state["review_length_note"] = length_note
        state["current_phase"] = "review_done"
        return state

    _report(progress_callback, f"准备综述证据：纳入 {len(evidence)} 条，跳过 {len(evidence_info['excluded'])} 条。")
    await _prepare_upload_digests(evidence, state, progress_callback)
    evidence_text = _prompt_evidence(evidence)
    graph_data = state.get("citation_graph_data") or {}
    raw_outline = await _call_prompt(
        "review.outline", topic=topic, focus=focus or "（未指定）", language=language,
        target_length=target, evidence=evidence_text,
    )
    themes = _parse_outline(raw_outline, evidence, graph_data)
    if not themes:
        themes = _fallback_themes(evidence, graph_data)
    state["review_outline"] = themes
    _report(progress_callback, f"提纲规划完成：{len(themes)} 个分主题，开始并行写作。")

    async def draft_one(theme: dict[str, Any]) -> str:
        theme_evidence = [by_id[i] for i in theme.get("evidence_ids", []) if i in by_id]
        text = await _call_prompt(
            "review.theme", theme_title=theme["title"], theme_question=theme.get("question", ""),
            topic=topic, focus=focus or "（未指定）", target_budget=max(500, target // max(1, len(themes))),
            evidence=_prompt_evidence(theme_evidence),
        )
        return text or _deterministic_theme(theme, by_id)

    theme_drafts = await asyncio.gather(*(draft_one(theme) for theme in themes))
    theme_drafts = [_validate_citations(text, set(by_id), excluded_ids) for text in theme_drafts]
    synthesis_raw = await _call_prompt(
        "review.synthesis", topic=topic, focus=focus or "（未指定）",
        theme_drafts="\n\n".join(f"## {t['title']}\n{d}" for t, d in zip(themes, theme_drafts)),
        evidence=evidence_text,
    )
    synthesis = _split_synthesis(synthesis_raw, _deterministic_synthesis(themes, by_id))
    synthesis = {k: _validate_citations(v, set(by_id), excluded_ids) for k, v in synthesis.items()}

    citations = "、".join(f"[{item['id']}]" for item in evidence[:8])
    intro = (
        f"围绕“{topic}”，本综述考察当前纳入的网络摘要与用户上传论文全文，重点分析研究问题、方法路径、主要发现及其演进关系。"
        f"研究材料显示该领域并非单一路线，代表证据包括 {citations}。"
        "全文级判断仅来自上传文件；网络论文的细节若未在摘要中出现，则明确标记为证据不足。"
    )
    scope = (
        f"本次默认/指定范围共纳入 {len(evidence)} 条证据：网络论文有效摘要 {evidence_info['network_abstract_count']} 条，"
        f"上传全文 {evidence_info['uploaded_fulltext_count']} 条；跳过 {len(evidence_info['excluded'])} 条。"
        "跳过原因包括摘要缺失或摘要能力关闭、上传 sidecar 不可解析，以及不属于当前会话的显式 id。"
        "因此，标题本身、历史网络全文缓存和模型常识均不作为综述证据。"
    )
    framework = "\n\n".join(
        f"### {theme['title']}\n\n{draft}" for theme, draft in zip(themes, theme_drafts)
    )
    conclusion = (
        f"综上，围绕“{topic}”的研究可按上述路径理解：方法差异塑造了发现的可比性，证据层级决定了结论的精细程度。"
        "现有材料支持对研究关系、共同限制和可复现方向作出综合判断，但不支持补写未报告的精确实验细节。"
    )
    refs: list[str] = []
    for item in evidence:
        if item["kind"] == "upload":
            refs.append(f"- **{item['filename']}** — 上传文件全文级证据（{item['char_count']} 字符）")
        else:
            link = f" ([Link]({item['landing_url']}))" if item.get("landing_url") else ""
            refs.append(f"- **{item['title']}**{link} — 网络论文摘要级证据")
    fallback = "\n\n".join([
        "# 引言", intro,
        "# 综述范围与证据基础", scope,
        "# 分类框架与分主题综述", framework,
        "# 方法、发现与发展脉络的综合比较", synthesis["comparison"],
        "# 主要争议、共同局限与研究空白", synthesis["gaps"],
        "# 未来研究方向", synthesis["future"],
        "# 结论", conclusion,
        "# 纳入文献与证据层级说明", "\n".join(refs),
    ])
    draft = _validate_citations(fallback, set(by_id), excluded_ids)
    revised = await _call_prompt(
        "review.revision", topic=topic, evidence=evidence_text,
        draft=draft, target_length=target,
    )
    revised = _validate_citations(revised, set(by_id), excluded_ids)
    final = _ensure_headings(revised, draft)
    current_length = _review_content_length(final)
    if current_length < max(1000, int(target * 0.65)):
        expanded = await _call_prompt(
            "review.expansion", topic=topic, target_length=target,
            current_length=current_length, evidence=evidence_text, draft=final,
        )
        expanded = _validate_citations(expanded, set(by_id), excluded_ids)
        if all(heading in expanded for heading in _REVIEW_HEADINGS) and _review_content_length(expanded) > current_length:
            final = expanded
            current_length = _review_content_length(final)
    final = _render_evidence_citations(final, by_id)
    state["literature_review"] = final.strip() + "\n"
    state["review_actual_target_length"] = target
    state["review_length_note"] = length_note
    state["review_stats"]["actual_length"] = current_length
    state["review_stats"]["target_coverage_ratio"] = round(current_length / target, 3) if target else 1.0
    state["current_phase"] = "review_done"
    _report(progress_callback, f"综述完成：正文约 {current_length} 字符；目标 {target}（{length_note}）。")
    return state


def _identify_gaps(papers: list[Paper], summaries: dict, topic: str) -> str:
    """Compatibility helper retained for older callers; no network full text."""
    gaps = []
    for paper in papers:
        summary = summaries.get(paper.id) if summaries else None
        for field in ("limitations", "future_work"):
            values = getattr(summary, field, []) if summary is not None else []
            if isinstance(values, list):
                gaps.extend(f"- [{paper.id}] {value}" for value in values[:2])
    return "\n".join(gaps[:15]) or "当前证据没有提供明确的研究空白。"
