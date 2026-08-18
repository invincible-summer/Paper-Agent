from __future__ import annotations
import asyncio
from types import SimpleNamespace
from core.models import Paper
from tools.search.router import RouteHints, choose_sources, infer_route_hints
from tools.search.registry import SOURCE_IDS, runtime_gate


def test_smart_route_cs_is_bounded_and_relevant():
    hints=infer_route_hints("large language model agent planning")
    decision=choose_sources(hints,list(SOURCE_IDS),set(SOURCE_IDS))
    assert decision.primary == ["dblp","arxiv","crossref","openalex"]
    assert len(decision.fallback) <= 2
    assert "semantic_scholar" in decision.fallback


def test_smart_route_medical_prefers_domain_sources():
    hints=infer_route_hints("clinical treatment patient outcomes")
    decision=choose_sources(hints,list(SOURCE_IDS),set(SOURCE_IDS))
    assert decision.primary == ["europepmc","pubmed","medrxiv","biorxiv"]


def test_explicit_source_is_respected_but_still_bounded():
    hints=RouteHints(disciplines=["general"],requested_sources=["hal"])
    decision=choose_sources(hints,list(SOURCE_IDS),set(SOURCE_IDS))
    assert decision.primary[0] == "hal"
    assert len(decision.primary) <= 4


def test_route_matrix_covers_repository_and_research_objects():
    repository = choose_sources(
        RouteHints(disciplines=["humanities"], query_intents=["repository"]),
        list(SOURCE_IDS), set(SOURCE_IDS),
    )
    assert repository.primary[:3] == ["openaire", "hal", "doaj"]
    assert repository.fallback[0] == "core"

    objects = choose_sources(
        RouteHints(disciplines=["general"], query_intents=["dataset"]),
        list(SOURCE_IDS), set(SOURCE_IDS),
    )
    assert objects.primary[:4] == ["datacite", "crossref", "openalex", "hal"]


def test_local_rule_infers_non_literature_intents():
    hints = infer_route_hints("search institutional repository technical reports")
    assert "repository" in hints.query_intents
    assert "report" in hints.query_intents


def test_conditional_license_gates():
    settings=SimpleNamespace(openalex_api_key="key",s2_api_key="key",s2_license_confirmed=False,
        core_api_key="key",core_license_confirmed=False,paper_platform_contact_email="x@example.org",
        crossref_email="",openalex_email="")
    assert runtime_gate("openalex",settings)==(True,"ready")
    assert runtime_gate("semantic_scholar",settings)==(False,"license_not_confirmed")
    assert runtime_gate("core",settings)==(False,"license_not_confirmed")


def test_empty_rxiv_index_is_not_routable(monkeypatch):
    import tools.search.rxiv_catalog as catalog
    monkeypatch.setattr(catalog, "sync_status", lambda source: {"indexed_count": 0})
    settings=SimpleNamespace(paper_platform_contact_email="",crossref_email="",openalex_email="")
    assert runtime_gate("biorxiv", settings) == (False, "index_empty")


def test_arxiv_batches_six_queries_into_at_most_two_requests(monkeypatch):
    from tools.search.arxiv import ArxivBackend
    backend=ArxivBackend(); calls=[]
    async def fake(search_query,limit):
        calls.append(search_query)
        return [Paper(id=f"P{len(calls)}",title=f"T{len(calls)}",source="arxiv")],200
    monkeypatch.setattr(backend,"_request",fake)
    outcome=asyncio.run(backend.search_many([f"query {i}" for i in range(6)],5))
    assert outcome.status=="ok"
    assert outcome.request_count==2
    assert len(calls)==2
    assert all(" OR " in q for q in calls)


def test_core_canonical_url_and_redirect_is_not_empty(monkeypatch):
    import tools.search.core as mod
    assert mod.API_URL.endswith("/v3/search/works/")
    class Resp:
        status_code=301; history=[]
        headers={"content-type":"text/html","location":mod.API_URL}
        def json(self): return {}
    class Client:
        async def request(self,*a,**k): return Resp()
    settings=SimpleNamespace(search=SimpleNamespace(core_api_key="key",core_license_confirmed=True))
    monkeypatch.setattr(mod,"get_settings",lambda:settings)
    monkeypatch.setattr(mod,"get_search_http_client",lambda:Client())
    outcome=asyncio.run(mod.CoreBackend().search_many(["graph"],5))
    assert outcome.status=="unexpected_redirect"
    assert outcome.http_status==301
    assert outcome.redirect_count==1


def test_core_schema_mismatch_is_not_reported_as_empty(monkeypatch):
    import tools.search.core as mod
    class Resp:
        status_code=200; history=[]; headers={"content-type":"application/json"}
        def json(self): return {"unexpected": []}
    class Client:
        async def request(self,*a,**k): return Resp()
    settings=SimpleNamespace(search=SimpleNamespace(core_api_key="key",core_license_confirmed=True))
    monkeypatch.setattr(mod,"get_settings",lambda:settings)
    monkeypatch.setattr(mod,"get_search_http_client",lambda:Client())
    outcome=asyncio.run(mod.CoreBackend().search_many(["graph"],5))
    assert outcome.status=="schema_mismatch"


def test_search_manager_defaults_to_smart():
    from tools.search.manager import SearchManager
    assert SearchManager([]).routing_mode == "smart"


def test_search_manager_returns_partial_results_at_hard_deadline(monkeypatch):
    import tools.search.manager as manager_mod
    from tools.search.base import SearchOutcome
    class Fast:
        async def search_many(self, queries, limit):
            return SearchOutcome("fast", [Paper(id="fast", title="Fast", source="fast")])
    class Slow:
        async def search_many(self, queries, limit):
            await asyncio.sleep(1)
            return SearchOutcome("slow", [])
    monkeypatch.setitem(manager_mod.BACKENDS, "fast", Fast())
    monkeypatch.setitem(manager_mod.BACKENDS, "slow", Slow())
    monkeypatch.setattr(manager_mod, "runtime_gate", lambda source: (True, "ready"))
    manager = manager_mod.SearchManager(["fast", "slow"], routing_mode="all_enabled",
                                        search_deadline_seconds=.05, per_source_timeout_seconds=.5)
    papers = asyncio.run(manager.search_all(["query"], topic="query"))
    assert [paper.id for paper in papers] == ["fast"]
    assert any(outcome.status == "local_budget_exhausted" for outcome in manager.last_outcomes)


def test_local_source_timeout_does_not_poison_breaker(monkeypatch):
    import tools.search.manager as manager_mod
    from core.search_source_health import get_search_health_registry
    class Hanging:
        async def search_many(self,queries,limit):
            await asyncio.Event().wait()
    monkeypatch.setitem(manager_mod.BACKENDS,"hang-local",Hanging())
    health=get_search_health_registry();health.reset()
    manager=manager_mod.SearchManager(["hang-local"],per_source_timeout_seconds=.01,search_deadline_seconds=.03)
    outcome=asyncio.run(manager._bounded_search("hang-local",Hanging(),["q"]))
    manager._record_health(outcome)
    assert outcome.status=="local_budget_exhausted"
    assert health.get("hang-local")["consecutive_failures"]==0


def test_rate_limiter_telemetry_counts_retry_requests():
    from tools.search.base import RateLimiter, SearchBackend
    class Resp:
        history=[]
        def __init__(self, status):
            self.status_code=status
            self.headers={"content-type":"application/json"}
            self.extensions={}
            self.url=SimpleNamespace(host="api.example.org")
    class Client:
        def __init__(self): self.calls=0
        async def request(self,*args,**kwargs):
            self.calls+=1
            return Resp(429 if self.calls == 1 else 200)
    async def run():
        limiter=RateLimiter(max_concurrent=1,min_interval=0,fast_fail_429=True)
        client=Client()
        async with limiter:
            response=await limiter.fetch(client,"GET","https://api.example.org",max_retries=3)
        backend=SearchBackend(); backend._reset_telemetry(); backend._capture_response(response)
        return client.calls, backend._last_request_count
    assert asyncio.run(run()) == (2, 2)


def test_explicit_ineligible_source_is_not_routed():
    hints=RouteHints(disciplines=["general"],requested_sources=["hal"])
    decision=choose_sources(hints,list(SOURCE_IDS),set(SOURCE_IDS)-{"hal"})
    assert "hal" not in decision.primary + decision.fallback


def test_smart_manager_uses_at_most_four_plus_two_sources(monkeypatch):
    import tools.search.manager as manager_mod
    from core.search_source_health import get_search_health_registry
    from tools.search.base import SearchOutcome
    called=[]
    primary={"dblp","arxiv","crossref","openalex"}
    class Fake:
        def __init__(self,name): self.name=name
        async def search_many(self,queries,limit):
            called.append(self.name)
            paper=(Paper(id="same",title="Same",doi="10.1/same",source=self.name)
                   if self.name in primary else
                   Paper(id=self.name,title=self.name,source=self.name))
            return SearchOutcome(self.name,[paper],"ok")
    for source in SOURCE_IDS:
        monkeypatch.setitem(manager_mod.BACKENDS,source,Fake(source))
    monkeypatch.setattr(manager_mod,"runtime_gate",lambda source:(True,"ready"))
    get_search_health_registry().reset()
    manager=manager_mod.SearchManager(list(SOURCE_IDS),routing_mode="smart",
                                      search_deadline_seconds=10,per_source_timeout_seconds=1)
    papers=asyncio.run(manager.search_all(["agent"],route_hints=RouteHints(disciplines=["cs"])))
    assert set(called)=={"dblp","arxiv","crossref","openalex","semantic_scholar","datacite"}
    assert len(manager.last_route["primary"])==4
    assert len(manager.last_route["fallback"])==2
    assert len(papers)==3


def test_arxiv_fifteen_queries_still_use_two_remote_requests(monkeypatch):
    from tools.search.arxiv import ArxivBackend, _limiter
    assert _limiter._min_interval == 3.0
    assert _limiter._semaphore._value == 1
    backend=ArxivBackend(); calls=[]
    async def fake(search_query,limit):
        calls.append(search_query)
        return [Paper(id=str(len(calls)),title=str(len(calls)),source="arxiv")],200
    monkeypatch.setattr(backend,"_request",fake)
    outcome=asyncio.run(backend.search_many([f"english query {i}" for i in range(15)],5))
    assert outcome.request_count==2
    assert len(calls)==2
    assert "query 8" not in " ".join(calls)


def test_openaire_oauth_token_cache_and_anonymous_budget(monkeypatch):
    import tools.search.openaire as mod
    class Resp:
        status_code=200
        def json(self): return {"access_token":"token","expires_in":600}
    class Client:
        def __init__(self): self.calls=0
        async def post(self,*args,**kwargs): self.calls+=1; return Resp()
    client=Client()
    settings=SimpleNamespace(search=SimpleNamespace(
        openaire_client_id="client",openaire_client_secret="secret"))
    monkeypatch.setattr(mod,"get_settings",lambda:settings)
    monkeypatch.setattr(mod,"get_search_http_client",lambda:client)
    mod._token=None
    assert asyncio.run(mod._access_token())=="token"
    assert asyncio.run(mod._access_token())=="token"
    assert client.calls==1
    mod._token=None
    mod._anonymous_calls.clear()
    try:
        assert all(mod._allow_anonymous() for _ in range(50))
        assert mod._allow_anonymous() is False
    finally:
        mod._anonymous_calls.clear()
