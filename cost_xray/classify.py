"""Shared read layer: reconcile canonical events against the wire `usage`, then roll
up (design.md §4 *Reconciliation*; docs/local/testing.md). Agent-agnostic — never branches
on agent. `cost.py` supplies the $ rates.

**Reconciliation principle.** The wire `usage` is ground truth for TOTALS; our tiktoken
event sizes are only PROPORTIONS (approximate for Claude). We *calibrate* — scale event
tokens so they sum to the wire total — then cut the calibrated input axis at the cache
boundary. So every roll-up equals the wire **exactly**; the only approximation is the
split between sibling leaves. The conservation laws this guarantees are tested in
docs/local/testing.md / tests/test_reconcile.py.

**Thinking is separated first** (design.md §6) — but the *why* is agent-specific, so the
correction factor is supplied by the caller (the adapter), never hardcoded here. For Claude,
tiktoken over-counts the base64 thinking `signature` ~2.6× (opposite-signed to its ~uniform
0.6× under-count elsewhere); left in the pool it drags the global scale and skews every bucket
(~−55%, measured). So we pin thinking to its real size first — `anchors["thinking"]` if
count_tokens supplied it, else `tiktoken × thinking_r` — and calibrate the rest to
`usage − thinking`. For agents whose tokenizer is exact (Codex/o200k, no base64 signature)
the adapter passes `thinking_r = 1.0`, which is a no-op (thinking rides the same scale, ≈1.0).
"""
from __future__ import annotations

from cost_xray import events as ev
from cost_xray.cost import rates

# LiteLLM exposes only the 5-min cache-write cost; the 1h-TTL write Claude Code uses is
# 2× input = 1.6× the 5-min rate. Applied on top of the per-model write rate.
_WRITE_1H_FACTOR = 1.6


_EMPTY_USAGE = {"fresh": 0, "cached": 0, "rewrote": 0, "output": 0, "write_1h": False}


def _calibrate(inp, total_in, anchors, thinking_r):
    """Scale tiktoken event sizes to a wire total, separating `thinking` first. Used for BOTH
    the input axis (`total = usage input`, optional static/messages `anchors`) and the output
    axis (`total = usage output`, `anchors=None`) — output `thinking` is the streamed signature
    and needs the same R-separation, else the uniform output scale over-counts it.

    `thinking` is pinned to its real size (`anchors["thinking"]` if count_tokens gave it, else
    `tiktoken × thinking_r` — the per-agent correction); everything else is calibrated to
    `usage − thinking`. With exact static/messages `anchors`, the non-thinking remainder is split
    exact-per-group (Static to its anchor, Messages to `messages − thinking`); else one global
    scale. `thinking_r = 1.0` (exact tokenizers) makes the separation a no-op.

    **Exact pins.** Any event carrying an `exact` token count (e.g. a tool schema counted exactly
    by `count_tokens`, cached since tools are static) is **pinned** to that value and taken out of
    the proportional pool — the remainder is scaled to `total − thinking − Σexact`. Same idea as
    thinking, but per-event: lets the displayed per-tool number be exact instead of
    tiktoken-proportional (verification.md). Conservation holds (Σ == total)."""
    is_think = [e["bucket"] == "thinking" for e in inp]
    raw_think = sum(e["tokens"] for e, t in zip(inp, is_think, strict=True) if t)
    think_total = (anchors or {}).get("thinking")
    if think_total is None:
        think_total = raw_think * thinking_r
    think_total = min(max(0.0, think_total), total_in)   # can't exceed the wire total (e.g. empty usage)
    if not raw_think:
        think_total = 0.0          # no thinking events to pin it to → don't steal it from the pool
    think_cal = [(e["tokens"] / raw_think * think_total) if (t and raw_think) else 0.0
                 for e, t in zip(inp, is_think, strict=True)]

    # per-event exact pins (e.g. exact per-tool count_tokens) — used as-is, out of the pool.
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
    """Split each ordered input span into (cached, rewrote, fresh) by intersecting it
    with the cache boundary [0,cached) [cached,cached+rewrote) [.., total)."""
    c_end, w_end = cached, cached + rewrote
    off, parts = 0.0, []
    for tok in sizes:
        s, e = off, off + tok
        parts.append((
            max(0.0, min(e, c_end) - s),                 # cached
            max(0.0, min(e, w_end) - max(s, c_end)),     # rewrote
            max(0.0, e - max(s, w_end)),                 # fresh
        ))
        off = e
    return parts


def reconcile_turn(events, usage, model, anchors=None, thinking_r=1.0, output_anchors=None):
    """Calibrate one turn's events to its wire `usage` and roll up. Returns
    {events, by_path, by_tool, by_mcp, bill, recon}.

    `usage` is **canonical** — `{fresh,cached,rewrote,output,write_1h}` from
    `adapters.usage(record)`. The shared layer no longer parses agent-specific usage
    field names; that lives per-agent in the adapter (design.md §9).

    `thinking_r` is the per-agent thinking-bucket correction (`adapters.thinking_r(agent)`):
    Claude ≈ 0.39 (tiktoken over-counts the base64 signature ~2.6×), Codex = 1.0 (o200k exact).
    The shared layer stays agent-agnostic — it just applies whatever factor it's handed.

    `anchors` (optional) are **exact** per-source token totals from Anthropic's
    `count_tokens` (`{static, messages, thinking?}`, count_tokens.py). When given, Static and
    Messages are each calibrated to their exact total — the per-source split becomes exact
    instead of tiktoken-proportional. Without it, a single global scale to the usage total.

    `output_anchors` (optional) does the same for the **output** axis — `{thinking}` from the exact
    output differencing (count_tokens.per_output_bucket_tokens). When given, output `thinking` is
    pinned to its exact size and the rest (text / tool_use) is tiktoken-proportioned to
    `output − thinking`, exactly mirroring the input thinking-pin — instead of the `thinking_r`
    estimate, which over/under-shoots and skews the text share (verification.md)."""
    u = {**_EMPTY_USAGE, **(usage or {})}
    inp = [e for e in events if e["zone"] == "input"]
    outp = [e for e in events if e["zone"] == "output"]
    raw_in = sum(e["tokens"] for e in inp)
    raw_out = sum(e["tokens"] for e in outp)
    total_in = u["fresh"] + u["cached"] + u["rewrote"]

    cal_in = _calibrate(inp, total_in, anchors, thinking_r)
    # output thinking is the streamed signature; pin it exactly when output_anchors given, else R.
    cal_out = _calibrate(outp, u["output"], output_anchors, thinking_r)
    cut = _cut(cal_in, u["cached"], u["rewrote"])

    r = rates(model)
    r_in, r_out = r["input"] / 1_000_000, r["output"] / 1_000_000
    r_cr = r["cache_read"] / 1_000_000                                  # per-model (LiteLLM)
    r_cw = (r["cache_write"] / 1_000_000) * (_WRITE_1H_FACTOR if u["write_1h"] else 1.0)

    enriched = []
    for e, cal, (c, w, f) in zip(inp, cal_in, cut, strict=True):
        cu, wu, fu = c * r_cr, w * r_cw, f * r_in     # cache_read $ / cache_write $ / fresh $
        enriched.append({**e, "cal_tokens": cal, "cached": c, "rewrote": w, "fresh": f,
                         "output": 0.0, "cached_usd": cu, "rewrote_usd": wu, "fresh_usd": fu,
                         "output_usd": 0.0, "usd": cu + wu + fu})
    for e, cal in zip(outp, cal_out, strict=True):
        ou = cal * r_out
        enriched.append({**e, "cal_tokens": cal, "cached": 0.0, "rewrote": 0.0, "fresh": 0.0,
                         "output": cal, "cached_usd": 0.0, "rewrote_usd": 0.0, "fresh_usd": 0.0,
                         "output_usd": ou, "usd": ou})

    roll = rollup(enriched)                             # by_path/category/cat_tool/tool/mcp + col + bill
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
    """Group already-enriched (calibrated, $-split) events into the summary breakdowns + the
    cache-state `columns` + `bill`. Shared by `reconcile_turn` (fresh, post-calibration) and the
    materializer's **refold-from-`derived`** (no re-tokenize) — both feed the same `summary` fold,
    so a fold-only change (e.g. a `category` relabel) re-aggregates from `derived` instead of
    re-tokenizing `raw`."""
    by_path, by_tool, by_mcp, by_category, by_cat_tool = {}, {}, {}, {}, {}
    for x in enriched:
        _accum(by_path, (x["zone"], x["section"], x["bucket"]), x)
        cat = ev.category(x)
        _accum(by_category, cat, x)                     # /context-aligned categories
        # per-(category, leaf) — lets the cost drill reach server→tool straight from `summary`
        # (only the per-turn level still reads `derived`). leaf = skill name, else tool, else —.
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
