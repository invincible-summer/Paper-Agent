#!/usr/bin/env python3
"""Measure the public OpenAI-compatible SSE chain without exposing secrets.

Usage:
  AGENT_API_URL=https://example.com/v1 AGENT_API_KEY=... \
    ./.env_conda/bin/python scripts/diagnose_openai_agent_stream.py \
    --prompt "搜索 retrieval augmented generation 的论文"

The script prints timing/frame metadata only. It never prints the credential,
request headers, complete response text, reasoning, tool payloads, or URLs from
attachments.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

import httpx


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose Paper Agent SSE timing")
    parser.add_argument("--base-url", default=os.getenv("AGENT_API_URL", "http://127.0.0.1:8000/v1"))
    parser.add_argument("--prompt", default="请简单介绍你自己。")
    parser.add_argument("--session-id", default=f"diag-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--timeout", type=float, default=115.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    key = os.getenv("AGENT_API_KEY", "").strip()
    if not key:
        print("AGENT_API_KEY is required (the value will not be printed).", file=sys.stderr)
        return 2

    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": "paper-agent",
        "stream": True,
        "sessionId": args.session_id,
        "messages": [{"role": "user", "content": args.prompt}],
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    started = time.monotonic()
    first_byte = None
    first_role = None
    first_reasoning = None
    first_content = None
    stop_at = None
    done_at = None
    last_frame = None
    max_idle = 0.0
    frames = 0
    data_bytes = 0
    finish_reason = None
    error_type = None

    try:
        timeout = httpx.Timeout(args.timeout, connect=min(10.0, args.timeout))
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if not content_type.startswith("text/event-stream"):
                    raise RuntimeError(f"unexpected content-type: {content_type!r}")
                for line in response.iter_lines():
                    now = time.monotonic()
                    if first_byte is None:
                        first_byte = now
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    data_bytes += len(line.encode("utf-8"))
                    if raw == "[DONE]":
                        done_at = now
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    frames += 1
                    if last_frame is not None:
                        max_idle = max(max_idle, now - last_frame)
                    last_frame = now
                    choices = chunk.get("choices") or []
                    choice = choices[0] if choices else {}
                    delta = choice.get("delta") or {}
                    if delta.get("role") == "assistant" and first_role is None:
                        first_role = now
                    if delta.get("reasoning") and first_reasoning is None:
                        first_reasoning = now
                    if delta.get("content") and first_content is None:
                        first_content = now
                    if choice.get("finish_reason") is not None:
                        finish_reason = choice.get("finish_reason")
                        stop_at = now
                    if isinstance(chunk.get("error"), dict):
                        error_type = chunk["error"].get("type")
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "ok": False,
            "error_type": type(exc).__name__,
            "message": str(exc)[:300],
        }, ensure_ascii=False, indent=2))
        return 1

    def elapsed(ts):
        return round(ts - started, 3) if ts is not None else None

    report = {
        "ok": done_at is not None and finish_reason in {"stop", "length"},
        "ttfb_seconds": elapsed(first_byte),
        "first_role_seconds": elapsed(first_role),
        "first_reasoning_seconds": elapsed(first_reasoning),
        "first_content_seconds": elapsed(first_content),
        "stop_seconds": elapsed(stop_at),
        "done_seconds": elapsed(done_at),
        "max_idle_gap_seconds": round(max_idle, 3),
        "frames": frames,
        "sse_data_bytes": data_bytes,
        "finish_reason": finish_reason,
        "error_type": error_type,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
