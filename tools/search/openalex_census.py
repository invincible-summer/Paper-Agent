"""Field census — OpenAlex group_by aggregation (deterministic macro statistics).

Answers "what does the WHOLE field look like" (yearly trend, top authors /
institutions / venues), in contrast to research_map which maps the structure
of the papers the user already has. Zero LLM for the aggregation; one utility
LLM pass writes a 3-sentence portrait (degrades to pure tables on failure).

Reuses the search backend RateLimiter + mailto discipline. Greenfield: the
codebase never used OpenAlex group_by before.
"""
from __future__ import annotations

import logging

import httpx

from core.config import get_settings
from core.llm import ainvoke_utility, get_llm
from core.prompts.registry import register
from tools.search.base import RateLimiter
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

API_URL = "https://api.openalex.org/works"
_limiter = RateLimiter(max_concurrent=5, min_interval=0.5)

_PORTRAIT_PROMPT = (
    "你是学术领域分析助手。下面是检索式「{query}」在 OpenAlex 上的宏观计量数据。"
    "用不超过 3 句话写一段领域画像，回答：该领域近年是在增长还是饱和/下滑、"
    "由谁（人或机构）主导、主要发表在哪些刊物。只基于给定的数据，不要编造未给出的数字或名字。\n\n"
    "年度发文量：{yearly}\n高产作者：{authors}\n高产机构：{institutions}\n主要刊物：{venues}"
)
register("field_census.portrait", 1, _PORTRAIT_PROMPT)


def _mailto() -> dict:
    email = get_settings().search.openalex_email
    return {"mailto": email} if email else {}


# ---------------------------------------------------------------------------
# Response parsing (pure, unit-tested)
# ---------------------------------------------------------------------------

def parse_groupby(groups: list) -> list[dict]:
    """Normalize an OpenAlex group_by response list to [{key, name, count}]."""
    out: list[dict] = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        name = g.get("key_display_name") or g.get("key") or ""
        if not name:
            continue
        out.append({
            "key": str(g.get("key") or ""),
            "name": str(name),
            "count": int(g.get("count") or 0),
        })
    return out


def sort_yearly_ascending(yearly: list[dict]) -> list[dict]:
    """Keep only integer-year buckets and sort ascending (for a trend line)."""
    def as_year(d):
        try:
            return int(d.get("key"))
        except (TypeError, ValueError):
            return None
    cleaned = [d for d in yearly if as_year(d) is not None]
    return sorted(cleaned, key=as_year)


# ---------------------------------------------------------------------------
# Aggregation (rate-limited, best-effort per dimension)
# ---------------------------------------------------------------------------

async def _groupby(client: httpx.AsyncClient, query: str, group_key: str,
                   per_page: int = 10, extra_filter: str = "") -> list:
    params = {"search": query, "group_by": group_key, "per-page": per_page, **_mailto()}
    if extra_filter:
        params["filter"] = extra_filter
    try:
        async with _limiter:
            resp = await _limiter.fetch(client, "GET", API_URL, params=params)
        if resp.status_code != 200:
            logger.debug("census group_by %s -> %s", group_key, resp.status_code)
            return []
        return resp.json().get("group_by") or []
    except Exception as e:  # noqa: BLE001
        logger.debug("census group_by %s failed: %s", group_key, e)
        return []


async def fetch_census(query: str) -> dict:
    """Aggregate the field across 4 dimensions. Each may degrade to [] independently."""
    import asyncio

    if not query:
        return {"yearly": [], "top_authors": [], "top_institutions": [], "top_venues": []}
    async with httpx.AsyncClient(timeout=30, proxy=None, trust_env=False) as client:
        yearly_raw, authors, insts, sources = await asyncio.gather(
            _groupby(client, query, "publication_year", per_page=20,
                     extra_filter="from_publication_date:2011-01-01"),
            _groupby(client, query, "authors.id", per_page=10),
            _groupby(client, query, "institutions.id", per_page=10),
            _groupby(client, query, "primary_location.source.id", per_page=10),
        )
    yearly = sort_yearly_ascending(parse_groupby(yearly_raw))
    return {
        "yearly": yearly,
        "top_authors": parse_groupby(authors)[:10],
        "top_institutions": parse_groupby(insts)[:10],
        "top_venues": parse_groupby(sources)[:10],
    }


# ---------------------------------------------------------------------------
# Portrait (1 utility LLM, best-effort)
# ---------------------------------------------------------------------------

def _compact(rows: list[dict], limit: int = 8) -> str:
    return ", ".join(f"{r['name']}({r['count']})" for r in rows[:limit]) or "（无数据）"


async def portrait(query: str, census: dict) -> str:
    """One light-LLM pass → ≤3-sentence field portrait. Empty string on failure."""
    if not any(census.values()):
        return ""
    try:
        llm = get_llm("light")
        from core.prompts.registry import get
        prompt = get("field_census.portrait").text.format(
            query=query,
            yearly=_compact(census.get("yearly", [])),
            authors=_compact(census.get("top_authors", [])),
            institutions=_compact(census.get("top_institutions", [])),
            venues=_compact(census.get("top_venues", [])),
        )
        resp = await ainvoke_utility(llm, [HumanMessage(content=prompt)])
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        return text
    except Exception as e:  # noqa: BLE001
        logger.debug("census portrait failed: %s", e)
        return ""
