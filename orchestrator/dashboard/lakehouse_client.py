"""Best-effort MCP client to the standalone lakehouse.

Hermes -> lakehouse is the ALLOWED direction of the decoupling boundary: Hermes is
an MCP *client* of `lakehouse-mcp`. This module never imports lakehouse code and
never opens its DuckDB file — it speaks MCP over stdio to the lakehouse's own server
process. Every entry point is timeout-bounded and returns a safe empty/None value on
ANY failure, so callers (recall enrichment, the dashboard tab) degrade gracefully
when the lakehouse is stopped or absent.

The public helpers are SYNCHRONOUS (they use asyncio.run internally). That is safe
from the sync MCP stdio server (recall) and, from the async FastAPI dashboard, must
be called via asyncio.to_thread so asyncio.run gets its own loop in a worker thread.

Config env: HERMES_LAKEHOUSE_MCP_CMD, HERMES_LAKEHOUSE_CONFIG, HERMES_LAKEHOUSE_TIMEOUT.
recall() auto-enrichment is additionally gated by HERMES_LAKEHOUSE_ENRICH (default off);
the dashboard tab is an explicit user action and is not gated.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

_CMD = os.environ.get(
    "HERMES_LAKEHOUSE_MCP_CMD",
    str(Path.home() / "dev/lakehouse/.venv/bin/lakehouse-mcp"))
_CONFIG = os.environ.get(
    "HERMES_LAKEHOUSE_CONFIG",
    str(Path.home() / "dev/lakehouse/config/lakehouse.toml"))
_TIMEOUT = float(os.environ.get("HERMES_LAKEHOUSE_TIMEOUT", "3.0"))

_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 60.0

# The metrics shown on the dashboard Lakehouse tab. (name, label, value_key, unit).
# value_key "_sum:<col>" sums that column across rows; otherwise it reads rows[0][key].
KEY_METRICS = [
    ("throughput", "Accepted tasks", "accepted_tasks", ""),
    ("velocity", "Velocity (accepted/sprint)", "_sum:velocity", ""),
    ("verification_coverage", "Verification coverage", "pct", "%"),
    ("blocked_count", "Blocked tasks", "blocked_tasks", ""),
    ("crash_rate", "Run crash rate", "pct", "%"),
    ("memory_health_pct", "Memory health", "pct", "%"),
]


def enabled() -> bool:
    """Kill switch for recall() auto-enrichment. Default OFF."""
    return os.environ.get("HERMES_LAKEHOUSE_ENRICH", "0") == "1"


def _available() -> bool:
    return os.path.exists(_CMD) and os.path.exists(_CONFIG)


async def _session_calls(calls: list[tuple[str, dict]]) -> list[dict | None]:
    """Open ONE stdio MCP session and run every (tool, args) call through it."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=_CMD, args=["--config", _CONFIG])
    out: list[dict | None] = []
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for tool, args in calls:
                try:
                    res = await s.call_tool(tool, args)
                    out.append(json.loads(res.content[0].text))
                except Exception:
                    out.append(None)
    return out


def invoke_many(calls: list[tuple[str, dict]], timeout: float = 10.0) -> list[dict | None]:
    """Run several tool calls in one session. Returns full server responses
    ({"result": ..., "as_of": ...} or {"error": ...}) or None per call on failure."""
    if not _available() or not calls:
        return [None] * len(calls)
    try:
        return asyncio.run(asyncio.wait_for(_session_calls(calls), timeout))
    except Exception:
        return [None] * len(calls)


def _result(resp: dict | None) -> dict | None:
    if isinstance(resp, dict) and "error" not in resp:
        return resp.get("result")
    return None


# --------------------------------------------------------------------------- #
# recall() enrichment
# --------------------------------------------------------------------------- #
def get_context_packet(task_id: str) -> dict | None:
    """Lakehouse context packet for a task via MCP, or None. TTL-cached."""
    if not task_id:
        return None
    hit = _CACHE.get(task_id)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    packet = _result(invoke_many([("get_context_packet", {"task_id": task_id})], _TIMEOUT)[0])
    if packet is not None:
        _CACHE[task_id] = (time.time(), packet)
    return packet


# --------------------------------------------------------------------------- #
# dashboard tab
# --------------------------------------------------------------------------- #
def overview() -> dict:
    """Key metrics + freshness for the Lakehouse tab. One MCP session for all metrics."""
    if not _available():
        return {"available": False, "reason": "lakehouse-mcp not found", "metrics": []}
    calls = [("get_metric", {"name": name}) for name, _, _, _ in KEY_METRICS]
    resp = invoke_many(calls, timeout=15.0)
    if all(r is None for r in resp):
        return {"available": False, "reason": "lakehouse unreachable", "metrics": []}

    metrics, as_of = [], None
    for (name, label, key, unit), r in zip(KEY_METRICS, resp):
        res = _result(r)
        value = None
        if res is not None:
            as_of = as_of or (r or {}).get("as_of")
            rows = res.get("rows") or []
            if key.startswith("_sum:"):
                col = key[5:]
                value = sum((row.get(col) or 0) for row in rows)
            elif rows:
                value = rows[0].get(key)
        metrics.append({"name": name, "label": label, "value": value, "unit": unit,
                        "definition": (res or {}).get("definition")})
    return {"available": True, "as_of": as_of, "metrics": metrics}


def _flatten_lineage(node: dict, edges: list, seen: set) -> None:
    """Walk a get_lineage tree (node = {name, kind, detail?, upstream:[…]}) and
    emit one source→target edge per upstream dependency, deduped."""
    if not isinstance(node, dict):
        return
    target = node.get("name")
    for up in node.get("upstream") or []:
        src = up.get("name")
        key = (src, target)
        if src and target and key not in seen:
            seen.add(key)
            edges.append({"source": src, "target": target,
                          "kind": up.get("kind") or "", "detail": up.get("detail") or ""})
        _flatten_lineage(up, edges, seen)


def lineage() -> dict:
    """Flattened dependency edges (gold→silver→bronze→source) across the tab's
    key metrics, for the Lakehouse lineage table. One MCP session for all calls;
    degrades to available:false when the lakehouse is absent/unreachable."""
    if not _available():
        return {"available": False, "reason": "lakehouse-mcp not found", "edges": []}
    calls = [("get_lineage", {"metric": name}) for name, _, _, _ in KEY_METRICS]
    resp = invoke_many(calls, timeout=20.0)
    if all(r is None for r in resp):
        return {"available": False, "reason": "lakehouse unreachable", "edges": []}
    edges, seen, as_of = [], set(), None
    for r in resp:
        as_of = as_of or (r or {}).get("as_of")
        res = _result(r)
        if res:
            _flatten_lineage(res, edges, seen)
    return {"available": True, "as_of": as_of, "edges": edges}


def ask(question: str) -> dict:
    """Proxy ask_lakehouse (constrained NL->metric) for the tab's search box."""
    if not question or not question.strip():
        return {"available": True, "declined": True, "reason": "empty question", "rows": []}
    resp = invoke_many([("ask_lakehouse", {"question": question})], timeout=25.0)[0]
    res = _result(resp)
    if res is None:
        return {"available": False, "reason": "lakehouse unreachable", "rows": []}
    res["available"] = True
    res["as_of"] = (resp or {}).get("as_of")
    return res
