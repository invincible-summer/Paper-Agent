"""Structured Trace for agent observability (DESIGN D-067, agent-develop skill V3).

Records the call chain / result chain / decision chain as JSONL so that for any
failure you can answer the three questions from the agent-develop skill:
  * what did it do?    (call chain)
  * what came back?    (result chain)
  * why did it decide? (decision chain)

Design principles (skill references/prompts-observability.md):
- Structured JSONL over text logs.
- Context complete: tool name+params, session state, token usage, model reaction.
- Never clean failure traces. The CONFLICT loop, the empty-search retry, the
  wrong-tool pick are the most valuable debugging evidence.
- Human + machine dual format: JSONL for machines, HTML for humans.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TRACE_DIR = _PROJECT_ROOT / "history_record" / "trace"


@dataclass
class TraceEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    span_id: str = ""
    parent_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ts": self.ts, "type": self.type, "data": self.data}
        if self.span_id:
            d["span_id"] = self.span_id
        if self.parent_id:
            d["parent_id"] = self.parent_id
        return d


class Span:
    """A timed, nested trace span. Use as a context manager."""

    def __init__(self, trace: "Trace", span_type: str, span_id: str, data: dict[str, Any]):
        self.trace = trace
        self.span_type = span_type
        self.span_id = span_id
        self.data = data
        self.start_ts = time.time()
        self.ended = False

    def __enter__(self) -> "Span":
        return self

    def end(self, **extra: Any) -> None:
        if self.ended:
            return
        self.ended = True
        latency_ms = round((time.time() - self.start_ts) * 1000, 1)
        self.trace.total_latency_ms += latency_ms
        end_data = dict(self.data)
        end_data["latency_ms"] = latency_ms
        end_data.update(extra)
        if self.trace._span_stack and self.trace._span_stack[-1] == self.span_id:
            self.trace._span_stack.pop()
        self.trace._write(TraceEvent(type=f"{self.span_type}_end", data=end_data, span_id=self.span_id))

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.end(status="error", error=f"{exc_type.__name__}: {exc}")
        else:
            self.end()
        return False


class Trace:
    """One Trace per agent turn. Writes JSONL and accumulates token/latency stats."""

    def __init__(self, trace_id: str | None = None, trace_dir: Path | str | None = None,
                 *, channel: str = "web", storage_context=None):
        self.trace_id = trace_id or uuid.uuid4().hex[:12]
        self.channel = channel
        self.storage_context = storage_context
        self._api_store = None
        self.mode = "full" if channel == "web" else "off"
        if channel == "openai_api" and storage_context is not None:
            from core.api_storage_store import ApiStorageStore
            self._api_store = ApiStorageStore(storage_context)
            self._api_store.initialize()
            self.mode = self._api_store.get_policy().trace_mode
            self.dir = storage_context.trace_dir
        else:
            self.dir = Path(trace_dir) if trace_dir else _TRACE_DIR
        if self.mode != "off":
            self.dir.mkdir(parents=True, exist_ok=True)
            if channel == "openai_api":
                os.chmod(self.dir, 0o700)
        self.path = self.dir / f"trace_{self.trace_id}.jsonl"
        self._fp = None
        self._closed = False
        self.events: list[dict[str, Any]] = []
        self.total_tokens = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_latency_ms = 0.0
        self._span_stack: list[str] = []

    @staticmethod
    def _redact(value: Any, *, metadata_only: bool = False) -> Any:
        sensitive_keys = {"authorization", "api_key", "key", "token", "password", "secret", "bytes", "raw", "full_text"}
        metadata_allowed = {"trace_id", "model", "tool", "status", "error_code", "latency_ms", "iterations", "tool_calls", "total_tokens", "input_tokens", "output_tokens", "total_latency_ms", "reason", "source"}
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                low = str(key).lower()
                if any(part in low for part in sensitive_keys):
                    out[key] = "[REDACTED]"
                elif metadata_only and key not in metadata_allowed:
                    continue
                else:
                    out[key] = Trace._redact(item, metadata_only=metadata_only)
            return out
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "[REDACTED_BYTES]"
        if isinstance(value, str):
            if value.startswith(("http://", "https://")):
                return value.split("?", 1)[0] + ("?[REDACTED]" if "?" in value else "")
            return re.sub(r"(?i)(bearer\s+|sk-|pa_live_)[A-Za-z0-9._~+/-]+", r"\1[REDACTED]", value)
        if isinstance(value, (list, tuple)):
            return [Trace._redact(item, metadata_only=metadata_only) for item in value]
        return value

    def _write(self, event: TraceEvent) -> None:
        if self._closed:
            return
        if self.channel == "openai_api" and self.mode == "off":
            return
        payload = event.to_dict()
        if self.channel == "openai_api":
            payload["data"] = self._redact(
                payload.get("data", {}), metadata_only=self.mode == "metadata"
            )
        if self._fp is None:
            self._fp = open(self.path, "a", encoding="utf-8")
            if self.channel == "openai_api":
                os.chmod(self.path, 0o600)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        self._fp.write(line + "\n")
        self._fp.flush()
        self.events.append(payload)

    def event(self, event_type: str, **data: Any) -> None:
        """Record a point-in-time event (no duration)."""
        parent = self._span_stack[-1] if self._span_stack else ""
        self._write(TraceEvent(type=event_type, data=data, parent_id=parent))

    def span(self, span_type: str, **data: Any) -> Span:
        """Open a timed span. Use as `with trace.span("llm_call", ...) as s:`."""
        span_id = uuid.uuid4().hex[:8]
        parent = self._span_stack[-1] if self._span_stack else ""
        self._write(TraceEvent(type=f"{span_type}_start", data=data, span_id=span_id, parent_id=parent))
        self._span_stack.append(span_id)
        return Span(self, span_type, span_id, data)

    def start(self, **data: Any) -> None:
        self.event("turn_start", trace_id=self.trace_id, **data)

    def finish(self, **data: Any) -> None:
        self.event(
            "turn_end",
            trace_id=self.trace_id,
            total_tokens=self.total_tokens,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_latency_ms=round(self.total_latency_ms, 1),
            **data,
        )
        self.close()
        if self.channel == "openai_api" and self._api_store is not None:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            is_error = 1 if data.get("status") == "error" else 0
            with self._api_store.connect() as conn:
                conn.execute(
                    "INSERT INTO api_trace_aggregates(day, requests, errors, input_tokens, output_tokens, total_tokens, total_latency_ms) "
                    "VALUES (?,1,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET requests=requests+1, errors=errors+excluded.errors, input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens, total_tokens=total_tokens+excluded.total_tokens, total_latency_ms=total_latency_ms+excluded.total_latency_ms",
                    (day, is_error, self.input_tokens, self.output_tokens, self.total_tokens, self.total_latency_ms),
                )
                if self.mode != "off" and self.path.is_file():
                    ttl = min(self._api_store.get_policy().trace_ttl_seconds, 7 * 24 * 3600)
                    conn.execute(
                        "INSERT OR REPLACE INTO api_trace_records(trace_id, mode, relative_path, created_at, expires_at, status, error_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (self.trace_id, self.mode, str(self.path.relative_to(self.storage_context.root_dir)), time.time(), time.time()+ttl, str(data.get("status") or "done"), str(data.get("error_code") or "") or None),
                    )
                conn.commit()

    def add_tokens(self, usage: dict[str, Any] | None) -> None:
        """Accumulate token usage from a LangChain usage_metadata / response dict."""
        if not usage:
            return
        inp = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
        out = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        self.input_tokens += int(inp)
        self.output_tokens += int(out)
        self.total_tokens += int(inp) + int(out)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._fp:
            self._fp.close()
            self._fp = None


def get_trace_dir() -> Path:
    """Default trace output directory (gitignored)."""
    return _TRACE_DIR


def _read_events(jsonl_path: str | Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def trace_to_html(jsonl_path: str | Path, out_path: str | Path | None = None) -> str:
    """Render a JSONL trace to a collapsible HTML for frame-by-frame replay.

    The human-facing format recommended by the agent-develop skill: open one
    HTML file and replay the agent step by step.
    """
    events = _read_events(jsonl_path)
    rows: list[str] = []
    for i, ev in enumerate(events):
        ts = ev.get("ts", 0)
        etype = html.escape(str(ev.get("type", "")))
        data = html.escape(json.dumps(ev.get("data", {}), ensure_ascii=False, default=str, indent=2))
        span_id = html.escape(str(ev.get("span_id", "")))
        parent_id = html.escape(str(ev.get("parent_id", "")))
        badge = ""
        if etype.endswith("_end"):
            badge = '<span class="b end">end</span>'
        elif etype.endswith("_start"):
            badge = '<span class="b start">start</span>'
        elif etype == "error":
            badge = '<span class="b err">error</span>'
        elif etype == "decision":
            badge = '<span class="b dec">decision</span>'
        elif etype == "warning":
            badge = '<span class="b warn">warn</span>'
        rows.append(
            f'<div class="frame">'
            f'<div class="hdr"><span class="idx">{i}</span>'
            f'<span class="ts">{ts:.3f}</span>'
            f'<span class="type">{etype}</span>{badge}'
            f'<span class="sid">{span_id}</span></div>'
            f'<pre class="data">{data}</pre>'
            f'<div class="pid">parent: {parent_id}</div></div>'
        )

    summary = ""
    turn_start = next((e for e in events if e.get("type") == "turn_start"), None)
    turn_end = next((e for e in events if e.get("type") == "turn_end"), None)
    if turn_start or turn_end:
        sd = turn_start.get("data", {}) if turn_start else {}
        ed = turn_end.get("data", {}) if turn_end else {}
        summary = (
            f'<div class="summary">'
            f'<b>trace_id</b>: {html.escape(str(sd.get("trace_id", "")))} | '
            f'<b>tokens</b>: {ed.get("total_tokens", "?")} '
            f'(in {ed.get("input_tokens", "?")} / out {ed.get("output_tokens", "?")}) | '
            f'<b>latency</b>: {ed.get("total_latency_ms", "?")} ms | '
            f'<b>iterations</b>: {ed.get("iterations", "?")} | '
            f'<b>tool_calls</b>: {ed.get("tool_calls", "?")}'
            f'</div>'
        )

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Trace {html.escape(str(Path(jsonl_path).stem))}</title>
<style>
body{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#0d1117;color:#c9d1d9;margin:0;padding:16px}}
.summary{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px;margin-bottom:14px;font-size:13px}}
.frame{{background:#161b22;border:1px solid #30363d;border-radius:6px;margin:8px 0}}
.hdr{{display:flex;gap:10px;align-items:center;padding:8px 10px;cursor:pointer;border-bottom:1px solid #21262d;font-size:13px}}
.idx{{color:#58a6ff;font-weight:700}}.ts{{color:#8b949e}}.type{{color:#f0f6fc;font-weight:600}}
.sid{{color:#6e7681;margin-left:auto}}.pid{{color:#484f58;font-size:11px;padding:0 10px 6px}}
.data{{margin:0;padding:8px 10px;color:#79c0ff;white-space:pre-wrap;font-size:12px;max-height:320px;overflow:auto}}
.b{{font-size:11px;padding:1px 6px;border-radius:8px;border:1px solid #30363d}}
.start{{color:#7ee787}}.end{{color:#8b949e}}.err{{color:#ff7b72}}.dec{{color:#d2a8ff}}.warn{{color:#e3b341}}
</style></head><body>
<h2 style="margin:0 0 4px">Paper Agent Trace</h2>
<div style="color:#8b949e;margin-bottom:8px">{html.escape(str(jsonl_path))}</div>
{summary}
{''.join(rows)}
<script>document.querySelectorAll('.frame').forEach(f=>{{const h=f.querySelector('.hdr');const d=f.querySelector('.data');let open=true;d.style.display='block';h.addEventListener('click',()=>{{open=!open;d.style.display=open?'block':'none'}})}})</script>
</body></html>"""
    if out_path is None:
        out_path = str(Path(jsonl_path).with_suffix(".html"))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path


def list_traces(limit: int = 50) -> list[dict[str, Any]]:
    """List recent trace files (newest first)."""
    d = _TRACE_DIR
    if not d.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("trace_*.jsonl"), reverse=True)[:limit]:
        try:
            evs = _read_events(p)
            ts_start = evs[0]["ts"] if evs else 0
            t_end = next((e for e in evs if e.get("type") == "turn_end"), None)
            out.append({
                "filename": p.name,
                "trace_id": p.stem.replace("trace_", ""),
                "events": len(evs),
                "total_tokens": (t_end or {}).get("data", {}).get("total_tokens", 0),
                "total_latency_ms": (t_end or {}).get("data", {}).get("total_latency_ms", 0),
                "ts_start": ts_start,
            })
        except Exception:
            continue
    return out
