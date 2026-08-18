"""bioRxiv/medRxiv local FTS backends; network sync is a separate scheduled job."""
from __future__ import annotations
import asyncio
from tools.search.base import SearchBackend, SearchOutcome
from tools.search.rxiv_catalog import search_catalog

class RxivBackend(SearchBackend):
    max_queries_per_turn = 8
    def __init__(self, server: str): self.name = server
    async def search(self, query: str, limit: int = 20):
        return await asyncio.to_thread(search_catalog, self.name, query, limit)
    async def search_many(self, queries: list[str], limit: int = 20) -> SearchOutcome:
        def run_local():
            papers=[]
            for q in queries[:8]: papers.extend(search_catalog(self.name, q, limit))
            return papers
        papers=await asyncio.to_thread(run_local)
        return SearchOutcome(self.name, papers, "ok" if papers else "reachable_empty", request_count=0)
