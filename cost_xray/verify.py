from __future__ import annotations

from cost_xray.analyze import ntok
from cost_xray.classify import reconcile_turn
from cost_xray.events import _count_content, unknown_types

INPUT_SOURCES = ("system", "tools", "static", "messages", "thinking")
_SYSTEM = ("system",)


def residuals(ours, truth):
    out = {}
    for k in sorted(set(ours) | set(truth), key=str):
        o = float(ours.get(k, 0.0))
        t = float(truth.get(k, 0.0))
        if t:
            out[k] = {"ours": o, "truth": t, "ratio": o / t,
                      "signed_rel": (o - t) / t, "abs_rel": abs(o - t) / t}
        else:
            out[k] = {"ours": o, "truth": t, "ratio": None,
                      "signed_rel": None, "abs_rel": None}
    return out


def ref_coord(ref):
    if not isinstance(ref, dict):
        return None
    field = ref.get("field")
    if field in ("system", "instructions"):
        return _SYSTEM
    if field == "tools":
        return ("tool", ref.get("i"))
    if "out" in ref:
        return ("out", ref["out"], ref["block"]) if "block" in ref else ("out", ref["out"])
    if "msg" in ref:
        return ("msg", ref["msg"], ref["block"]) if "block" in ref else ("msg", ref["msg"])
    return None


def coverage(record, adapter, events=None):
    events = adapter.to_events(record) if events is None else events
    units = list(adapter.raw_units(record))

    expected = {coord for coord, _c in units}
    covered, orphan = set(), []
    for e in events:
        coord = ref_coord(e.get("ref"))
        if coord is None:
            orphan.append(e.get("ref"))
        else:
            covered.add(coord)
    missing = sorted(expected - covered, key=str)
    stray = sorted(covered - expected, key=str)

    indep_nonsys = sum(ntok(_count_content(c)) for coord, c in units if coord != _SYSTEM)
    ev_nonsys = sum(e["tokens"] for e in events if ref_coord(e.get("ref")) not in (None, _SYSTEM))
    indep_sys = sum(ntok(_count_content(c)) for coord, c in units if coord == _SYSTEM)
    ev_sys = sum(e["tokens"] for e in events if ref_coord(e.get("ref")) == _SYSTEM)

    token_delta = ev_nonsys - indep_nonsys
    return {
        "expected": len(expected),
        "covered": len(expected & covered),
        "missing": missing,
        "orphan": orphan + stray,
        "unknown_types": sorted(unknown_types(events)),
        "token_exact_delta": token_delta,
        "system_token_delta": ev_sys - indep_sys,
        "ok": not missing and not orphan and not stray and token_delta == 0,
    }


def _is_input_system(e):
    return e["zone"] == "input" and (e["bucket"] == "system"
                                     or (e["bucket"] == "schema" and e.get("skill")))


def _is_input_tool(e):
    return (e["zone"] == "input" and e["section"] == "static"
            and e["bucket"] == "schema" and not e.get("skill"))


def _msg_bucket(b):
    return "tool_io" if b in ("tool_use", "tool_result") else b


def local_truths(events):
    def s(p):
        return sum(e["tokens"] for e in events if p(e))

    sys_t, tools_t = s(_is_input_system), s(_is_input_tool)
    msg_t = s(lambda e: e["zone"] == "input" and e["section"] == "messages")
    anchors = {"system": sys_t, "tools": tools_t, "static": sys_t + tools_t, "messages": msg_t,
               "thinking": s(lambda e: e["zone"] == "input" and e["bucket"] == "thinking")}
    per_tool = [(f"tool:{e['tool']}", e["tokens"]) for e in events
                if _is_input_tool(e) and e.get("tool")]
    per_bucket = {}
    for e in events:
        if e["zone"] == "input" and e["section"] == "messages":
            k = _msg_bucket(e["bucket"])
            per_bucket[k] = per_bucket.get(k, 0) + e["tokens"]
    per_output = [(e["bucket"], e["tokens"]) for e in events if e["zone"] == "output"]
    return {"anchors": anchors, "per_tool": per_tool, "per_bucket": per_bucket,
            "per_output": per_output, "total_in": sys_t + tools_t + msg_t,
            "total_out": sum(e["tokens"] for e in events if e["zone"] == "output")}


def _csum(enriched, pred):
    return sum(e["cal_tokens"] for e in enriched if pred(e))


def bench_turn(record, model, adapter, *, anchors=None, per_tool=None, per_bucket=None,
               per_output=None, per_message=None, per_output_event=None,
               output_thinking=None, thinking_r=1.0, pin_tools=False):
    events = adapter.to_events(record)
    usage = adapter.usage(record)
    if pin_tools and per_tool:
        name2exact = {lbl[5:]: tok for lbl, tok in per_tool if lbl.startswith("tool:")}
        for e in events:
            if _is_input_tool(e) and e.get("tool") in name2exact:
                e["exact"] = name2exact[e["tool"]]
    out_anchors = {"thinking": output_thinking} if output_thinking is not None else None
    r = reconcile_turn(events, usage, model, thinking_r=thinking_r, output_anchors=out_anchors)
    E = r["events"]

    facets = {}

    if anchors:
        ours_src = {
            "system": _csum(E, _is_input_system),
            "tools": _csum(E, _is_input_tool),
            "messages": _csum(E, lambda e: e["zone"] == "input" and e["section"] == "messages"),
            "thinking": _csum(E, lambda e: e["zone"] == "input" and e["bucket"] == "thinking"),
        }
        ours_src["static"] = ours_src["system"] + ours_src["tools"]
        truth_src = {k: anchors[k] for k in INPUT_SOURCES if k in anchors}
        facets["input_source"] = _facet(ours_src, truth_src)

    if per_tool:
        ours_tool = {}
        for e in E:
            if _is_input_tool(e) and e.get("tool"):
                ours_tool[e["tool"]] = ours_tool.get(e["tool"], 0.0) + e["cal_tokens"]
        truth_tool = {lbl[5:]: tok for lbl, tok in per_tool if lbl.startswith("tool:")}
        facets["input_tool"] = _facet(ours_tool, truth_tool)

    if per_bucket:
        ours_bkt = {}
        for e in E:
            if e["zone"] == "input" and e["section"] == "messages":
                k = _msg_bucket(e["bucket"])
                ours_bkt[k] = ours_bkt.get(k, 0.0) + e["cal_tokens"]
        truth_bkt = {k: v for k, v in per_bucket.items() if k != "structure"}
        struct = per_bucket.get("structure", 0)
        tot = sum(truth_bkt.values())
        if struct and tot:
            truth_bkt = {k: v + struct * (v / tot) for k, v in truth_bkt.items()}
        facets["input_bucket"] = _facet(ours_bkt, truth_bkt)

    if per_output:
        ours_out = {}
        for e in E:
            if e["zone"] == "output":
                k = _msg_bucket(e["bucket"])
                ours_out[k] = ours_out.get(k, 0.0) + e["cal_tokens"]
        truth_out = {}
        for bkt, tok in per_output:
            truth_out[_msg_bucket(bkt)] = truth_out.get(_msg_bucket(bkt), 0.0) + tok
        facets["output_bucket"] = _facet(ours_out, truth_out)

    if per_message:
        ours_msg = {}
        for e in E:
            if e["zone"] == "input" and e["section"] == "messages":
                key = (("tool_io", e.get("id")) if e["bucket"] in ("tool_use", "tool_result")
                       else ref_coord(e.get("ref")))
                ours_msg[key] = ours_msg.get(key, 0.0) + e["cal_tokens"]
        truth_msg = {tuple(coord) if isinstance(coord, list) else coord: tok
                     for coord, _bkt, tok in per_message}
        facets["input_message"] = _facet(ours_msg, truth_msg)

    if per_output_event:
        ours_oe = [e for e in E if e["zone"] == "output"
                   and e["bucket"] in ("text", "thinking", "redacted_thinking")]
        ours_out_e, truth_out_e = {}, {}
        for k, ((tbkt, ttok), e) in enumerate(zip(per_output_event, ours_oe, strict=False)):
            key = ("out", k, tbkt)
            ours_out_e[key] = e["cal_tokens"]
            truth_out_e[key] = ttok
        facets["output_event"] = _facet(ours_out_e, truth_out_e)

    return {
        "meta": {"model": model, "input_tokens": r["recon"]["wire"]["total_input"],
                 "output_tokens": r["recon"]["wire"]["output"]},
        "facets": facets,
        "reconstruction": {"input_err": r["recon"]["approx"]["input_err"],
                           "output_err": r["recon"]["approx"]["output_err"]},
        "coverage": coverage(record, adapter, events),
    }


def _facet(ours, truth):
    return {"ours": ours, "truth": truth, "residual": residuals(ours, truth)}


def _pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _stats(rows):
    ar = [a for _s, a in rows]
    sr = [s for s, _a in rows if s is not None]
    return {"n": len(rows), "abs_rel_p50": _pct(ar, 0.5), "abs_rel_p90": _pct(ar, 0.9),
            "abs_rel_max": max(ar) if ar else None,
            "signed_rel_mean": (sum(sr) / len(sr)) if sr else None}


def aggregate(turn_reports):
    facet_rows = {}
    facet_key_rows = {}
    for rep in turn_reports:
        for fname, f in rep.get("facets", {}).items():
            for key, res in f["residual"].items():
                if res["abs_rel"] is None:
                    continue
                facet_rows.setdefault(fname, []).append((res["signed_rel"], res["abs_rel"]))
                facet_key_rows.setdefault(fname, {}).setdefault(str(key), []).append(
                    (res["signed_rel"], res["abs_rel"]))

    facets = {}
    for fname, rows in facet_rows.items():
        facets[fname] = {**_stats(rows),
                         "by_key": {k: _stats(r) for k, r in facet_key_rows[fname].items()}}

    rec_in = [rep["reconstruction"]["input_err"] for rep in turn_reports if "reconstruction" in rep]
    rec_out = [rep["reconstruction"]["output_err"] for rep in turn_reports
               if "reconstruction" in rep]
    reconstruction = {
        "input": {"n": len(rec_in), "p50": _pct(rec_in, 0.5), "p90": _pct(rec_in, 0.9),
                  "max": max(rec_in) if rec_in else None},
        "output": {"n": len(rec_out), "p50": _pct(rec_out, 0.5), "p90": _pct(rec_out, 0.9),
                   "max": max(rec_out) if rec_out else None},
    }

    covs = [rep["coverage"] for rep in turn_reports if "coverage" in rep]
    cov = {
        "turns": len(covs),
        "ok": sum(1 for c in covs if c["ok"]),
        "with_missing": sum(1 for c in covs if c["missing"]),
        "with_orphan": sum(1 for c in covs if c["orphan"]),
        "token_mismatch": sum(1 for c in covs if c["token_exact_delta"] != 0),
        "unknown_types": sorted({t for c in covs for t in c["unknown_types"]}),
    }
    return {"facets": facets, "reconstruction": reconstruction, "coverage": cov}


_FACET_LABEL = {
    "input_tool": "input · schema (per tool)",
    "input_message": "input · message (per event)",
    "output_event": "output (per event)",
    "input_bucket": "input · message (per bucket)",
    "output_bucket": "output (per bucket)",
    "input_source": "input · source (system / static / thinking)",
}
_FACET_ORDER = ("input_tool", "input_message", "output_event",
                "input_bucket", "output_bucket", "input_source")


def render_markdown(agg, title="event-token precision"):
    def f(x):
        return "—" if x is None else f"{x * 100:.1f}%"

    facets = agg.get("facets", {})
    lines = [f"## {title} — per-event accuracy (calibrated vs count_tokens differencing)", ""]
    if any(facets.get(n) for n in _FACET_ORDER):
        lines += ["| facet | n | p50 | p90 | max | bias |", "|---|--:|--:|--:|--:|--:|"]
        for name in _FACET_ORDER:
            s = facets.get(name)
            if not s:
                continue
            lines.append(f"| {_FACET_LABEL[name]} | {s['n']} | {f(s['abs_rel_p50'])} "
                         f"| {f(s['abs_rel_p90'])} | {f(s['abs_rel_max'])} | {f(s['signed_rel_mean'])} |")
    else:
        lines.append("_no per-event truth — o200k is this agent's own tokenizer; total only._")

    rc = agg.get("reconstruction")
    if rc:
        lines += ["", "## total reconstruction (raw tiktoken vs exact wire `usage`), |rel|", "",
                  "| axis | n | p50 | p90 | max |", "|---|--:|--:|--:|--:|",
                  f"| input | {rc['input']['n']} | {f(rc['input']['p50'])} "
                  f"| {f(rc['input']['p90'])} | {f(rc['input']['max'])} |",
                  f"| output | {rc['output']['n']} | {f(rc['output']['p50'])} "
                  f"| {f(rc['output']['p90'])} | {f(rc['output']['max'])} |"]

    c = agg["coverage"]
    lines += ["", "## completeness", "",
              f"- turns checked: **{c['turns']}**, fully ok: **{c['ok']}**",
              f"- turns with a missing block: **{c['with_missing']}**, "
              f"orphan ref: **{c['with_orphan']}**, token mismatch: **{c['token_mismatch']}**",
              f"- unmapped wire types (tripwire): {c['unknown_types'] or 'none'}"]
    return "\n".join(lines)
