from __future__ import annotations
import asyncio
from datetime import date


def test_fetch_interval_uses_official_api_and_cursor(monkeypatch):
    import scripts.sync_rxiv_metadata as sync
    seen=[]; saved=[]
    class Resp:
        status_code=200
        def __init__(self,records): self.records=records
        def json(self): return {"collection":self.records}
    class Client:
        async def request(self,method,url,**kwargs):
            seen.append(url)
            return Resp([{"doi":f"10.1101/{len(seen)}","title":"T"}])
    monkeypatch.setattr(sync,"get_search_http_client",lambda:Client())
    monkeypatch.setattr(sync,"upsert_records",lambda server,records:saved.extend(records) or len(records))
    changed,complete,cursor=asyncio.run(sync.fetch_interval("biorxiv",date(2024,1,1),date(2024,1,1),10**12))
    assert changed==1 and complete is True and cursor==0 and len(seen)==1 and "/details/biorxiv/2024-01-01/2024-01-01/0/json" in seen[0]
    assert saved[0]["title"]=="T"


def test_fetch_interval_returns_resumable_page_cursor(monkeypatch):
    import scripts.sync_rxiv_metadata as sync
    seen=[]
    class Resp:
        status_code=200
        def __init__(self, records): self._records=records
        def json(self): return {"collection": self._records}
    class Client:
        async def request(self, method, url, **kwargs):
            seen.append(url)
            await asyncio.sleep(.02)
            return Resp([{"doi":"10.1101/a","title":"A"}, {"doi":"10.1101/b","title":"B"}])
    monkeypatch.setattr(sync, "get_search_http_client", lambda: Client())
    monkeypatch.setattr(sync, "upsert_records", lambda server, records: len(records))
    monkeypatch.setattr(sync, "PAGE_SIZE", 2)
    changed, complete, cursor = asyncio.run(sync.fetch_interval(
        "biorxiv", date(2024,1,1), date(2024,1,1), sync.time.monotonic() + .01,
    ))
    assert (changed, complete, cursor) == (2, False, 2)
    assert seen[0].endswith("/0/json")


def test_sync_main_splits_one_global_budget_between_servers(monkeypatch):
    import scripts.sync_rxiv_metadata as sync
    deadlines=[]
    async def fake_sync(server, max_seconds, *, deadline=None):
        deadlines.append((server, deadline))
        return {"indexed_count":0,"min_published":None,"max_published":None,"changed":0}
    async def fake_close(): pass
    monkeypatch.setattr(sync, "sync_server", fake_sync)
    monkeypatch.setattr(sync, "close_search_http_clients", fake_close)
    args=type("Args", (), {"server":["biorxiv","medrxiv"],"max_seconds":900})()
    started=sync.time.monotonic()
    assert asyncio.run(sync.amain(args)) == 0
    assert [row[0] for row in deadlines] == ["biorxiv","medrxiv"]
    assert deadlines[0][1] <= started + 451
    assert deadlines[1][1] <= started + 901
