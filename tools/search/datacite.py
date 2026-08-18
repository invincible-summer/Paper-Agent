"""DataCite DOI metadata backend (official REST API, metadata-only)."""
from __future__ import annotations
import httpx
from core.models import Paper
from tools.search.base import SearchBackend, generate_paper_id, shorten_chinese_query, RateLimiter
from tools.search.http_client import get_search_http_client

API_URL = "https://api.datacite.org/dois"
_limiter = RateLimiter(max_concurrent=2, min_interval=0.5, fast_fail_429=True)

def parse_results(data: dict) -> list[Paper]:
    out=[]
    for item in data.get("data", []) or []:
        attrs=item.get("attributes") or {}
        titles=attrs.get("titles") or []
        title=next((str(x.get("title") or "").strip() for x in titles if x.get("title")), "")
        if not title: continue
        authors=[]
        for c in attrs.get("creators") or []:
            name=c.get("name") or " ".join(filter(None,[c.get("givenName"),c.get("familyName")]))
            if name: authors.append(str(name).strip())
        doi=(attrs.get("doi") or item.get("id") or "").lower() or None
        year=attrs.get("publicationYear")
        try: year=int(year) if year else None
        except (TypeError,ValueError): year=None
        descriptions=attrs.get("descriptions") or []
        abstract=next((d.get("description") for d in descriptions if (d.get("descriptionType") or "").lower()=="abstract"), "") or ""
        subjects=[s.get("subject") for s in attrs.get("subjects") or [] if s.get("subject")]
        landing_url = attrs.get("url") or (f"https://doi.org/{doi}" if doi else "")
        out.append(Paper(id=generate_paper_id(title,authors[0] if authors else "",year,doi),title=title,
            authors=authors,year=year,venue=attrs.get("publisher") or "",doi=doi,source="datacite",
            abstract=abstract,pdf_url=None,keywords=subjects,urls={"datacite":landing_url} if landing_url else {}))
    return out

class DataCiteBackend(SearchBackend):
    name="datacite"
    async def search(self, query: str, limit: int=20)->list[Paper]:
        params={"query":shorten_chinese_query(query),"resource-type-id":"text","page[size]":min(limit,50)}
        try:
            async with _limiter:
                resp=await _limiter.fetch(get_search_http_client(),"GET",API_URL,params=params)
            self._capture_response(resp)
            if resp.status_code!=200:return []
            data=resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                self._forced_status="schema_mismatch"
                return []
            return parse_results(data)
        except httpx.TimeoutException:
            self._forced_status="timeout"; return []
        except httpx.RequestError:
            self._forced_status="connection_error"; return []
        except (ValueError,TypeError):
            self._forced_status="schema_mismatch"; return []
