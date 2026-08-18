"""DBLP computer-science publication search through the official JSON API."""
from __future__ import annotations
import httpx
from core.models import Paper
from tools.search.base import SearchBackend, generate_paper_id, RateLimiter
from tools.search.http_client import get_search_http_client
from tools.search.registry import contact_email
from core.config import get_settings

API_URLS=("https://dblp.org/search/publ/api","https://dblp.dagstuhl.de/search/publ/api")
_limiter=RateLimiter(max_concurrent=1,min_interval=1.0,fast_fail_429=True)

def _list(value):
    if value is None:return []
    return value if isinstance(value,list) else [value]

def parse_results(data:dict)->list[Paper]:
    hits=((data.get("result") or {}).get("hits") or {}).get("hit") or []
    out=[]
    for hit in _list(hits):
        info=hit.get("info") or {}; title=str(info.get("title") or "").rstrip(".").strip()
        if not title:continue
        author_nodes=((info.get("authors") or {}).get("author") or [])
        authors=[]
        for a in _list(author_nodes):
            name=a.get("text") if isinstance(a,dict) else a
            if name:authors.append(str(name))
        year=info.get("year")
        try:year=int(year) if year else None
        except (TypeError,ValueError):year=None
        doi=(info.get("doi") or "").lower() or None
        url=info.get("url") or ""
        if url.startswith("db/"):url="https://dblp.org/rec/"+url[3:]
        out.append(Paper(id=generate_paper_id(title,authors[0] if authors else "",year,doi),title=title,
            authors=authors,year=year,venue=info.get("venue") or "",doi=doi,source="dblp",
            abstract="",pdf_url=None,urls={"dblp":url} if url else {}))
    return out

class DblpBackend(SearchBackend):
    name="dblp"
    async def search(self,query:str,limit:int=20)->list[Paper]:
        email=contact_email(get_settings().search)
        ua=f"PaperAgent/1.0 ({email})" if email else "PaperAgent/1.0"
        try:
            resp=None
            for url in API_URLS:
                async with _limiter:
                    resp=await _limiter.fetch(get_search_http_client(),"GET",url,
                        params={"q":query,"format":"json","h":min(limit,50)},headers={"User-Agent":ua})
                self._capture_response(resp)
                if resp.status_code < 500:
                    break
            if resp is None or resp.status_code!=200:return []
            data=resp.json()
            hits=(data.get("result") or {}).get("hits") if isinstance(data,dict) else None
            hit=hits.get("hit") if isinstance(hits,dict) else None
            if not isinstance(hits,dict) or (hit is not None and not isinstance(hit,(dict,list))):
                self._forced_status="schema_mismatch"
                return []
            return parse_results(data)
        except httpx.TimeoutException:
            self._forced_status="timeout"; return []
        except httpx.RequestError:
            self._forced_status="connection_error"; return []
        except (ValueError,TypeError):
            self._forced_status="schema_mismatch"; return []
