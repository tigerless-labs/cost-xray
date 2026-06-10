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
    return _cat_tool_rows(session_dirs, group, label, by_server=True)


def cat_breakdown(session_dirs, group, label, server=None):
    return _cat_tool_rows(session_dirs, group, label, server=server)


def cat_calls(session_dirs, group, label, tool=None):
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


def _in_cat(events, group, label):
    return [e for e in events if _category(e) == (group, label)]


def ctx_servers(events, group, label):
    agg = defaultdict(lambda: {"tokens": 0, "n": 0})
    for e in _in_cat(events, group, label):
        srv = _mcp_server(e.get("tool")) or "—"
        agg[srv]["tokens"] += e.get("tokens", 0)
        agg[srv]["n"] += 1
    rows = [{"label": k, **v} for k, v in agg.items()]
    rows.sort(key=lambda r: -r["tokens"])
    return rows


def ctx_breakdown(events, group, label, server=None):
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
    out = []
    for e in _in_cat(events, group, label):
        if tool is not None and (e.get("skill") or e.get("tool") or "—") != tool:
            continue
        out.append({"tokens": e.get("tokens", 0), "ref": e.get("ref"), "id": e.get("id")})
    out.sort(key=lambda r: -r["tokens"])
    return out


def fetch_content(session_dir, ref):
    if not isinstance(ref, dict):
        return ""
    d = pathlib.Path(session_dir)
    records = list(raw_codec.iter_records(d))
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
