from __future__ import annotations

from cost_xray import events as ev
from cost_xray.cost import rates

_WRITE_1H_FACTOR = 1.6


_EMPTY_USAGE = {"fresh": 0, "cached": 0, "rewrote": 0, "output": 0, "write_1h": False}


def _calibrate(inp, total_in, anchors, thinking_r):
    is_think = [e["bucket"] == "thinking" for e in inp]
    raw_think = sum(e["tokens"] for e, t in zip(inp, is_think, strict=True) if t)
    think_total = (anchors or {}).get("thinking")
    if think_total is None:
        think_total = raw_think * thinking_r
    think_total = min(max(0.0, think_total), total_in)
    if not raw_think:
        think_total = 0.0
    think_cal = [(e["tokens"] / raw_think * think_total) if (t and raw_think) else 0.0
                 for e, t in zip(inp, is_think, strict=True)]

    is_exact = [(not t) and isinstance(e.get("exact"), (int, float))
                for e, t in zip(inp, is_think, strict=True)]
    exact_cal = [float(e["exact"]) if x else 0.0 for e, x in zip(inp, is_exact, strict=True)]

    def _exact_in(section):
        return sum(c for e, x, c in zip(inp, is_exact, exact_cal, strict=True)
                   if x and e["section"] == section)

    if anchors and (anchors.get("static") or anchors.get("messages")):
        raw_s = sum(e["tokens"] for e, t, x in zip(inp, is_think, is_exact, strict=True)
                    if not t and not x and e["section"] == "static") or 1
        raw_m = sum(e["tokens"] for e, t, x in zip(inp, is_think, is_exact, strict=True)
                    if not t and not x and e["section"] == "messages") or 1
        sc_s = max(0.0, anchors.get("static", 0) - _exact_in("static")) / raw_s
        sc_m = max(0.0, anchors.get("messages", 0) - think_total - _exact_in("messages")) / raw_m
        rest_cal = [e["tokens"] * (sc_s if e["section"] == "static" else sc_m) for e in inp]
    else:
        raw_rest = sum(e["tokens"] for e, t, x in zip(inp, is_think, is_exact, strict=True)
                       if not t and not x) or 1
        sc = max(0.0, total_in - think_total - sum(exact_cal)) / raw_rest
        rest_cal = [e["tokens"] * sc for e in inp]

    return [tc if t else (xc if x else rc)
            for t, x, tc, xc, rc in zip(is_think, is_exact, think_cal, exact_cal, rest_cal,
                                        strict=True)]


def _cut(sizes, cached, rewrote):
    c_end, w_end = cached, cached + rewrote
    off, parts = 0.0, []
    for tok in sizes:
        s, e = off, off + tok
        parts.append((
            max(0.0, min(e, c_end) - s),
            max(0.0, min(e, w_end) - max(s, c_end)),
            max(0.0, e - max(s, w_end)),
        ))
        off = e
    return parts


def reconcile_turn(events, usage, model, anchors=None, thinking_r=1.0, output_anchors=None):
    u = {**_EMPTY_USAGE, **(usage or {})}
    inp = [e for e in events if e["zone"] == "input"]
    outp = [e for e in events if e["zone"] == "output"]
    raw_in = sum(e["tokens"] for e in inp)
    raw_out = sum(e["tokens"] for e in outp)
    total_in = u["fresh"] + u["cached"] + u["rewrote"]

    cal_in = _calibrate(inp, total_in, anchors, thinking_r)
    cal_out = _calibrate(outp, u["output"], output_anchors, thinking_r)
    cut = _cut(cal_in, u["cached"], u["rewrote"])

    r = rates(model)
    r_in, r_out = r["input"] / 1_000_000, r["output"] / 1_000_000
    r_cr = r["cache_read"] / 1_000_000
    r_cw = (r["cache_write"] / 1_000_000) * (_WRITE_1H_FACTOR if u["write_1h"] else 1.0)

    enriched = []
    for e, cal, (c, w, f) in zip(inp, cal_in, cut, strict=True):
        cu, wu, fu = c * r_cr, w * r_cw, f * r_in
        enriched.append({**e, "cal_tokens": cal, "cached": c, "rewrote": w, "fresh": f,
                         "output": 0.0, "cached_usd": cu, "rewrote_usd": wu, "fresh_usd": fu,
                         "output_usd": 0.0, "usd": cu + wu + fu})
    for e, cal in zip(outp, cal_out, strict=True):
        ou = cal * r_out
        enriched.append({**e, "cal_tokens": cal, "cached": 0.0, "rewrote": 0.0, "fresh": 0.0,
                         "output": cal, "cached_usd": 0.0, "rewrote_usd": 0.0, "fresh_usd": 0.0,
                         "output_usd": ou, "usd": ou})

    roll = rollup(enriched)
    recon = {
        "wire": {"fresh": u["fresh"], "cached": u["cached"], "rewrote": u["rewrote"],
                 "output": u["output"], "total_input": total_in},
        "ours": {"input_tokens": sum(x["cal_tokens"] for x in enriched if x["zone"] == "input"),
                 "output_tokens": sum(x["cal_tokens"] for x in enriched if x["zone"] == "output"),
                 "columns": roll["columns"]},
        "approx": {"raw_input": raw_in, "raw_output": raw_out,
                   "scale_out": (u["output"] / raw_out) if raw_out else 0.0,
                   "exact": bool(anchors),
                   "input_err": abs(raw_in - total_in) / total_in if total_in else 0.0,
                   "output_err": abs(raw_out - u["output"]) / u["output"] if u["output"] else 0.0},
        "bill": roll["bill"],
    }
    return {"events": enriched, **roll, "recon": recon}


def rollup(enriched):
    by_path, by_tool, by_mcp, by_category, by_cat_tool = {}, {}, {}, {}, {}
    for x in enriched:
        _accum(by_path, (x["zone"], x["section"], x["bucket"]), x)
        cat = ev.category(x)
        _accum(by_category, cat, x)
        _accum(by_cat_tool, (*cat, x.get("skill") or x.get("tool") or "—"), x)
        if x.get("tool"):
            _accum(by_tool, x["tool"], x)
            srv = ev.mcp_server(x["tool"])
            if srv:
                _accum(by_mcp, srv, x)
    col = {k: sum(x[k] for x in enriched) for k in ("cached", "rewrote", "fresh", "output")}
    return {"by_path": by_path, "by_category": by_category, "by_cat_tool": by_cat_tool,
            "by_tool": by_tool, "by_mcp": by_mcp, "columns": col,
            "bill": sum(x["usd"] for x in enriched)}


_ACCUM_FIELDS = ("cached", "rewrote", "fresh", "output",
                 "cached_usd", "rewrote_usd", "fresh_usd", "output_usd")


def _accum(dst, key, x):
    d = dst.setdefault(key, {"tokens": 0.0, "usd": 0.0, **{k: 0.0 for k in _ACCUM_FIELDS}})
    d["tokens"] += x["cal_tokens"]
    d["usd"] += x["usd"]
    for k in _ACCUM_FIELDS:
        d[k] += x[k]
