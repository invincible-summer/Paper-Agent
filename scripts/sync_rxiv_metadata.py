#!/usr/bin/env python3
"""Incrementally mirror official bioRxiv/medRxiv metadata into local FTS5.

No HTML/search-page scraping and no PDF download is performed.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from tools.search.base import RateLimiter  # noqa: E402
from tools.search.http_client import get_search_http_client, close_search_http_clients  # noqa: E402
from tools.search.rxiv_catalog import START_DATES, sync_status, update_sync_state, upsert_records  # noqa: E402

API_TEMPLATE = "https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}/json"
_LIMITER = RateLimiter(max_concurrent=1, min_interval=3.0, fast_fail_429=True)
PAGE_SIZE = 100


async def fetch_interval(server: str, start: date, end: date, deadline: float,
                         *, cursor: int = 0) -> tuple[int, bool, int]:
    cursor = max(0, int(cursor)); changed = 0; complete = False
    while time.monotonic() < deadline:
        url = API_TEMPLATE.format(server=server, start=start.isoformat(), end=end.isoformat(), cursor=cursor)
        async with _LIMITER:
            response = await _LIMITER.fetch(get_search_http_client(), "GET", url)
        if response.status_code != 200:
            raise RuntimeError(f"official_api_http_{response.status_code}")
        payload = response.json()
        records = payload.get("collection")
        if not isinstance(records, list):
            raise RuntimeError("official_api_schema_mismatch")
        changed += upsert_records(server, records)
        cursor += len(records)
        if len(records) < PAGE_SIZE:
            complete = True
            break
    return changed, complete, (0 if complete else cursor)


async def sync_server(server: str, max_seconds: int, *, deadline: float | None = None) -> dict:
    if server not in START_DATES: raise ValueError("invalid server")
    deadline = deadline if deadline is not None else time.monotonic() + max(1, max_seconds)
    today = date.today(); recent_start = today - timedelta(days=7)
    changed = 0
    try:
        recent_changed, _, _ = await fetch_interval(server, recent_start, today, deadline)
        changed += recent_changed
        state = sync_status(server)
        backfill = date.fromisoformat(state["backfill_date"] or START_DATES[server].isoformat())
        backfill_cursor = int(state.get("backfill_cursor") or 0)
        while backfill < recent_start and time.monotonic() < deadline:
            day_changed, complete, next_cursor = await fetch_interval(
                server, backfill, backfill, deadline, cursor=backfill_cursor,
            )
            changed += day_changed
            if not complete:
                backfill_cursor = next_cursor
                update_sync_state(server, next_date=backfill, cursor=backfill_cursor)
                break
            backfill += timedelta(days=1)
            backfill_cursor = 0
            update_sync_state(server, next_date=backfill, cursor=0)
        update_sync_state(server, next_date=backfill, cursor=backfill_cursor)
        result = sync_status(server); result["changed"] = changed
        return result
    except Exception as exc:
        update_sync_state(server, error=type(exc).__name__ + ":" + str(exc)[:160])
        raise


async def amain(args) -> int:
    output = {}
    global_deadline = time.monotonic() + max(1, args.max_seconds)
    try:
        for index, server in enumerate(args.server):
            now = time.monotonic()
            if now >= global_deadline:
                row = sync_status(server); row["changed"] = 0; output[server] = row
                continue
            remaining_servers = len(args.server) - index
            server_deadline = min(global_deadline, now + (global_deadline - now) / remaining_servers)
            output[server] = await sync_server(server, args.max_seconds, deadline=server_deadline)
        # Status only: no raw records, URLs, or credentials.
        for server, row in output.items():
            print(f"{server}: indexed={row['indexed_count']} coverage={row['min_published'] or '-'}..{row['max_published'] or '-'} changed={row['changed']}")
        return 0
    finally:
        await close_search_http_clients()


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--server", action="append", choices=sorted(START_DATES), help="repeatable; default both")
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--scheduled", action="store_true", help="document that invocation is timer-driven")
    args=parser.parse_args(); args.server=args.server or ["biorxiv","medrxiv"]
    if args.max_seconds < 30:
        parser.error("--max-seconds must be at least 30")
    return asyncio.run(amain(args))

if __name__ == "__main__": raise SystemExit(main())
