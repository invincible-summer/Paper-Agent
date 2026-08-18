"""Shared per-event-loop HTTP clients for official academic APIs."""
from __future__ import annotations

import asyncio
import weakref

import httpx

_CLIENTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = weakref.WeakKeyDictionary()


def get_search_http_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _CLIENTS.get(loop)
    if client is None or client.is_closed:
        timeout = httpx.Timeout(12.0, connect=4.0, read=10.0, write=5.0, pool=2.0)
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=12, keepalive_expiry=30.0)
        client = httpx.AsyncClient(
            timeout=timeout, limits=limits, follow_redirects=False,
            proxy=None, trust_env=False,
        )
        _CLIENTS[loop] = client
    return client


async def close_search_http_clients() -> None:
    clients = list(_CLIENTS.values())
    _CLIENTS.clear()
    for client in clients:
        if not client.is_closed:
            await client.aclose()
