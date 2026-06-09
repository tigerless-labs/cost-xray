"""Drill-down + lazy content fetch for the TUI (design.md §4, *lazy content fetch*).

Reads the materialized `derived.jsonl` (per-event `tokens` + `ref`) for the breakdown,
and reaches into `raw.jsonl` (the truth) only on demand to pull the actual text a `ref`
points at — so a panel row can expand to per-tool → per-call → the real output without
ever pre-loading raw. Framework-agnostic and testable; the Textual layer just renders
what these return.
"""
from __future__ import annotations

import json
import pathlib
from collections import defaultdict

from cost_xray import adapters, raw_codec
from cost_xray.events import category as _category
from cost_xray.events import mcp_server as _mcp_server


def _norm(session_dirs):
    return list(session_dirs) if isinstance(session_dirs, (list, tuple)) else [session_dirs]


def _derived_one(session_dir):
    p = pathlib.Path(session_dir) / "derived.jsonl"
    if not p.exists():
        return []
    turns = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                turns.append(json.loads(line))
            except Exception:
                pass
    return turns


def bucket_breakdown(session_dirs, zone, section, bucket):
    """Per-tool/skill totals inside one (zone, section, bucket) cell, fattest first.
    `session_dirs` is one dir or a list (agent aggregate — sums across sessions).
    Expands e.g. `tool_result` → Read / Bash / Grep. Returns [{label, tokens, n}]."""
    agg = defaultdict(lambda: {"tokens": 0, "n": 0})
    for d in _norm(session_dirs):
        for turn in _derived_one(d):
            for e in turn.get("events", []):
                if e.get("zone") == zone and e.get("section") == section and e.get("bucket") == bucket:
                    label = e.get("tool") or e.get("skill") or "—"
                    agg[label]["tokens"] += e.get("tokens", 0)
                    agg[label]["n"] += 1
    rows = [{"label": k, **v} for k, v in agg.items()]
    rows.sort(key=lambda r: -r["tokens"])
    return rows


def tool_calls(session_dirs, zone, section, bucket, tool=None):
    """Individual events (calls / blocks) in a cell, fattest first. With `tool`, only that
    tool's events; with `tool=None`, **every** event of the bucket — used to drill the
    tool-less buckets (thinking / text / system) straight to per-turn. Each row carries its
    `dir` so `fetch_content` knows where to read. Returns [{turn, tokens, ref, id, dir}]."""
    out = []
    for d in _norm(session_dirs):
        for turn in _derived_one(d):
            for e in turn.get("events", []):
                if (e.get("zone") != zone or e.get("section") != section
                        or e.get("bucket") != bucket):
                    continue
                if tool is not None and (e.get("tool") or e.get("skill")) != tool:
                    continue
                out.append({"turn": turn.get("turn"), "tokens": e.get("tokens", 0),
                            "ref": e.get("ref"), "id": e.get("id"), "dir": str(d)})
    out.sort(key=lambda r: -r["tokens"])
    return out


# Cost drill reads the **pre-aggregated** `summary.by_cat_tool` (category → leaf, cumulative),
# NOT `derived` — so server→tool is O(categories), not a full derived re-scan. Only the per-turn
# level (`cat_calls`) still reads `derived`. by_cat_tool keys are "group|label|leaf" (leaf =
# skill name, else tool, else —); server is derived from the leaf (mcp__<srv>__ prefix).
_SUM_FIELDS = ("cached_usd", "rewrote_usd", "fresh_usd", "output_usd")


def _summary_one(session_dir):
    p = pathlib.Path(session_dir) / "summary.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _cat_tool_rows(session_dirs, group, label, *, server=None, by_server=False):
    """Aggregate `summary.by_cat_tool` for one category into per-server (`by_server=True`) or
    per-leaf rows, optionally filtered to one MCP `server`. Carries the cache-$ split so a
    server/tool row shows 读/写/新 like its parent. Fattest $ first."""
    agg = defaultdict(lambda: {"tokens": 0.0, "usd": 0.0, **{k: 0.0 for k in _SUM_FIELDS}})
    for d in _norm(session_dirs):
        for k, v in _summary_one(d).get("by_cat_tool", {}).items():
            parts = k.split("|", 2) if isinstance(k, str) else list(k)
            if len(parts) != 3:
                continue
            g, lbl, leaf = parts
            if g != group or lbl != label:
                continue
            srv = _mcp_server(leaf) or "—"
            if server is not None and srv != server:
                continue
            a = agg[srv if by_server else leaf]
            a["tokens"] += v.get("tokens", 0.0)
            a["usd"] += v.get("usd", 0.0)
            for f in _SUM_FIELDS:
                a[f] += v.get(f, 0.0)
    rows = [{"label": kk, **vv} for kk, vv in agg.items()]
    rows.sort(key=lambda r: -r["usd"])
    return rows


def cat_servers(session_dirs, group, label):
    """Per-MCP-**server** within a category (from `summary`) — the level above per-tool for MCP
    buckets, so an MCP category drills server → tool → call. `[{label=server, tokens, usd, *_usd}]`."""
    return _cat_tool_rows(session_dirs, group, label, by_server=True)


def cat_breakdown(session_dirs, group, label, server=None):
    """Per-tool/skill within a `/context` category `(group, label)` (from `summary`) — for drilling
    a **cost** row (e.g. `("Messages","system tool use+output") → Bash / Read / …`). With `server`,
    only that MCP server's tools. `[{label, tokens, usd, *_usd}]`, fattest $ first."""
    return _cat_tool_rows(session_dirs, group, label, server=server)


def cat_calls(session_dirs, group, label, tool=None):
    """Individual events (turn occurrences) within a category, optionally one tool. Each carries
    its `dir` for `fetch_content`. Returns `[{turn, tokens, usd, ref, dir}]`, fattest $ first."""
    out = []
    for d in _norm(session_dirs):
        for turn in _derived_one(d):
            for e in turn.get("events", []):
                if _category(e) != (group, label):
                    continue
                if tool is not None and (e.get("skill") or e.get("tool") or "—") != tool:
                    continue
                out.append({"turn": turn.get("turn"), "tokens": e.get("tokens", 0),
                            "usd": e.get("usd", 0.0), "ref": e.get("ref"), "dir": str(d)})
    out.sort(key=lambda r: -r["usd"])
    return out


# --- context drill: same server→tool→call levels, but over ONE turn's in-memory events ------
# The cost table drills the cumulative `derived.jsonl`; the context table is a single-turn
# **window snapshot** (the latest derived line, already in memory), so these take an `events`
# list instead of reading disk — same shape as `cat_*`, tokens-only (no $). Keeps the two
# tables' drill identical while the context one never re-reads a file.

def _in_cat(events, group, label):
    return [e for e in events if _category(e) == (group, label)]


def ctx_servers(events, group, label):
    """Per-MCP-server within a category, for one turn's events. `[{label=server, tokens, n}]`."""
    agg = defaultdict(lambda: {"tokens": 0, "n": 0})
    for e in _in_cat(events, group, label):
        srv = _mcp_server(e.get("tool")) or "—"
        agg[srv]["tokens"] += e.get("tokens", 0)
        agg[srv]["n"] += 1
    rows = [{"label": k, **v} for k, v in agg.items()]
    rows.sort(key=lambda r: -r["tokens"])
    return rows


def ctx_breakdown(events, group, label, server=None):
    """Per-tool/skill within a category, for one turn's events (optional MCP-server filter).
    Skill name first (so Skills drills to skill names). `[{label, tokens, n}]`."""
    agg = defaultdict(lambda: {"tokens": 0, "n": 0})
    for e in _in_cat(events, group, label):
        if server is not None and _mcp_server(e.get("tool")) != server:
            continue
        k = e.get("skill") or e.get("tool") or "—"
        agg[k]["tokens"] += e.get("tokens", 0)
        agg[k]["n"] += 1
    rows = [{"label": k, **v} for k, v in agg.items()]
    rows.sort(key=lambda r: -r["tokens"])
    return rows


def ctx_calls(events, group, label, tool=None):
    """Individual blocks within a category for one turn, each with its `ref` for `fetch_content`.
    `[{tokens, ref, id}]`, fattest first."""
    out = []
    for e in _in_cat(events, group, label):
        if tool is not None and (e.get("skill") or e.get("tool") or "—") != tool:
            continue
        out.append({"tokens": e.get("tokens", 0), "ref": e.get("ref"), "id": e.get("id")})
    out.sort(key=lambda r: -r["tokens"])
    return out


def fetch_content(session_dir, ref):
    """Lazily fetch the real text a `ref` points at, from `raw.jsonl`. `ref` is the
    adapter-native locator: `{turn,msg[,block]}` / `{turn,out}` / `{turn,field[,i]}`. The
    raw-shape-specific lookup lives in the adapter (`adapters.locate`); this layer only reads
    `raw`, dispatches by agent (the session dir's parent), and renders the text. Returns ''
    if unresolvable."""
    if not isinstance(ref, dict):
        return ""
    d = pathlib.Path(session_dir)
    records = list(raw_codec.iter_records(d))           # streams + reconstructs deltas (legacy passes through)
    return _block_text(adapters.locate(records, ref, agent=d.parent.name))


def _block_text(b):
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    if isinstance(b, list):
        return "\n".join(_block_text(x) for x in b)
    if isinstance(b, dict):
        c = b.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(_block_text(x) for x in c)
        return (b.get("text") or b.get("thinking") or b.get("output")
                or json.dumps(b, ensure_ascii=False))
    return str(b)
