"""OpenAIRE Graph API V3 research-product search backend."""
from __future__ import annotations
import time
from collections import deque
import httpx
from core.config import get_settings
from core.models import Paper
from tools.search.base import SearchBackend, SearchOutcome, generate_paper_id, shorten_chinese_query, RateLimiter
from tools.search.http_client import get_search_http_client

API_URL="https://api.openaire.eu/graph/v3/research-products"
TOKEN_URL="https://aai.openaire.eu/oidc/token"
_limiter=RateLimiter(max_concurrent=2,min_interval=1.0,fast_fail_429=True)
_token: tuple[str,float] | None=None
_anonymous_calls: deque[float] = deque()

def _allow_anonymous() -> bool:
    now=time.time()
    while _anonymous_calls and now-_anonymous_calls[0]>=3600: _anonymous_calls.popleft()
    if len(_anonymous_calls)>=50: return False
    _anonymous_calls.append(now); return True

def _first(value):
    if isinstance(value,list): return _first(value[0]) if value else ""
    if isinstance(value,dict):
        nested = value.get("$") or value.get("value") or value.get("name") or value.get("fullName") or value.get("title") or value.get("id") or ""
        return _first(nested)
    return str(value or "")

def _doi(item:dict)->str|None:
    for pid in item.get("pids") or item.get("identifiers") or []:
        if isinstance(pid,dict) and str(pid.get("scheme") or pid.get("type") or "").lower()=="doi":
            return _first(pid).lower() or None
    return None

def parse_results(data:dict)->list[Paper]:
    """Parse the current Graph API V3 ``results`` shape only."""
    out=[]
    for item in data.get("results") or []:
        title=_first(item.get("mainTitle") or item.get("title"))
        if not title:continue
        authors=[_first(a) for a in item.get("authors") or item.get("creators") or [] if _first(a)]
        date=_first(item.get("publicationDate") or item.get("dateOfAcceptance") or item.get("date"))
        year=int(date[:4]) if date[:4].isdigit() else None
        doi=_doi(item); rid=_first(item.get("id"))
        out.append(Paper(id=generate_paper_id(title,authors[0] if authors else "",year,doi),title=title,
            authors=authors,year=year,venue=_first(item.get("publisher") or item.get("journal")),doi=doi,
            source="openaire",abstract=_first(item.get("description") or item.get("descriptions")),pdf_url=None,
            urls={"openaire":f"https://explore.openaire.eu/search/publication?pid={rid}"} if rid else {}))
    return out


def _valid_response_schema(data: object) -> bool:
    return isinstance(data, dict) and isinstance(data.get("results"), list)

async def _access_token()->str:
    global _token
    s=get_settings().search
    if not s.openaire_client_id or not s.openaire_client_secret:return ""
    if _token and _token[1]>time.time()+60:return _token[0]
    resp=await get_search_http_client().post(TOKEN_URL,data={"grant_type":"client_credentials"},
        auth=(s.openaire_client_id,s.openaire_client_secret))
    if resp.status_code!=200:return ""
    data=resp.json(); token=str(data.get("access_token") or ""); ttl=int(data.get("expires_in") or 300)
    if token:_token=(token,time.time()+ttl)
    return token

class OpenAireBackend(SearchBackend):
    name="openaire"
    async def search(self,query:str,limit:int=20)->list[Paper]:
        params={"search":shorten_chinese_query(query),"type":"publication","page":1,"pageSize":min(limit,50)}
        try:
            token=await _access_token(); headers={"Authorization":f"Bearer {token}"} if token else None
            if not token and not _allow_anonymous():
                self._forced_status="local_budget_exhausted"
                return []
            async with _limiter:
                resp=await _limiter.fetch(get_search_http_client(),"GET",API_URL,params=params,headers=headers)
            self._capture_response(resp)
            if resp.status_code!=200:return []
            data=resp.json()
            if not _valid_response_schema(data):
                self._forced_status="schema_mismatch"
                return []
            return parse_results(data)
        except httpx.TimeoutException:
            self._forced_status="timeout"; return []
        except httpx.RequestError:
            self._forced_status="connection_error"; return []
        except (ValueError,TypeError):
            self._forced_status="schema_mismatch"; return []
