from __future__ import annotations

import fcntl
import json
import os
import pathlib

from cost_xray import adapters, count_tokens, raw_codec
from cost_xray.adapters import anthropic as _anthropic
from cost_xray.classify import _WRITE_1H_FACTOR, reconcile_turn, rollup
from cost_xray.cost import rates

LOGIC_VERSION = 10


def _exact_pins(obj, evs, model, agent, path):
    in_anchors = out_anchors = None
    use_ct = (adapters.adapter_for(agent=agent, path=path) is _anthropic
              and count_tokens.auth_headers() is not None)
    try:
        ot = adapters.output_thinking(obj, agent=agent, path=path)
        if ot is None and use_ct:
            blocks = adapters.response_blocks(obj, agent=agent, path=path)
            if blocks:
                ot = count_tokens.output_thinking_tokens(blocks, model)
        if ot is not None:
            out_anchors = {"thinking": ot}
    except Exception:
        pass
    if use_ct:
        try:
            msgs = (obj.get("request") or {}).get("messages")
            it = count_tokens.input_thinking_tokens(msgs, model)
            if it:
                in_anchors = {"thinking": it}
        except Exception:
            pass
    if use_ct:
        try:
            units = dict(adapters.raw_units(obj, agent=agent, path=path))
            tool_evs = sorted((e for e in evs if e.get("bucket") == "schema"
                               and (e.get("ref") or {}).get("field") == "tools"),
                              key=lambda e: (e.get("ref") or {}).get("i", 0))
            tools = [units.get(("tool", (e.get("ref") or {}).get("i"))) for e in tool_evs]
            if tool_evs and all(isinstance(t, dict) for t in tools):
                vals = count_tokens.tools_exact(tools, model)
                if vals and len(vals) == len(tool_evs):
                    for e, v in zip(tool_evs, vals, strict=True):
                        e["exact"] = v
        except Exception:
            pass
    return in_anchors, out_anchors


def _atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _parse_lines(lines):
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _derived_event(e):
    return {
        "zone": e["zone"], "section": e["section"], "bucket": e["bucket"],
        "tool": e.get("tool"), "skill": e.get("skill"), "role": e.get("role"),
        "tokens": round(e["cal_tokens"]),
        "cached": round(e["cached"]), "rewrote": round(e["rewrote"]),
        "fresh": round(e["fresh"]), "output": round(e["output"]),
        "usd": e["usd"],
        "ref": e.get("ref"), "id": e.get("id"), "type": e.get("type"), "hash": e.get("hash"),
    }


def _derived_line(turn, obj, model, win, usage, r):
    return {
        "turn": turn, "ts": obj.get("ts"), "model": model, "window": win, "usage": usage,
        "bill": r["bill"],
        "events": [_derived_event(e) for e in r["events"]],
    }


MIN_REFOLD = 10


def _enrich_from_derived(line):
    model = line.get("model") or ""
    rt = rates(model)
    r_in, r_out, r_cr = rt["input"] / 1e6, rt["output"] / 1e6, rt["cache_read"] / 1e6
    r_cw = (rt["cache_write"] / 1e6) * (_WRITE_1H_FACTOR if (line.get("usage") or {}).get("write_1h") else 1.0)
    out = []
    for e in line.get("events", []):
        c, w, f, o = e.get("cached", 0), e.get("rewrote", 0), e.get("fresh", 0), e.get("output", 0)
        out.append({**e, "cal_tokens": e.get("tokens", 0),
                    "cached_usd": c * r_cr, "rewrote_usd": w * r_cw,
                    "fresh_usd": f * r_in, "output_usd": o * r_out})
    return model, out


def _refold(d, agent, old):
    summary = _empty_summary(agent, d.name)
    for k in ("name", "project"):
        if old.get(k):
            summary[k] = old[k]
    turn = 0
    for line in (d / "derived.jsonl").read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        model, enriched = _enrich_from_derived(obj)
        roll = rollup(enriched)
        _fold(summary, {**roll, "recon": {"ours": {"columns": roll["columns"]}}}, model)
        turn += 1
    summary["n_turns"] = turn
    summary["raw_offset"] = old.get("raw_offset", 0)
    _atomic_write(d / "summary.json", json.dumps(summary, ensure_ascii=False))
    return summary


def _load_state(d, agent):
    sp, dp = d / "summary.json", d / "derived.jsonl"
    if sp.exists() and dp.exists():
        try:
            sm = json.loads(sp.read_text())
            n = sum(1 for ln in dp.read_text().splitlines() if ln.strip())
            if sm.get("agent") == agent and sm.get("n_turns") == n:
                if sm.get("logic_version") == LOGIC_VERSION:
                    return sm, n, sm.get("raw_offset", 0)
                if sm.get("logic_version", 0) >= MIN_REFOLD and adapters.incremental(agent=agent):
                    return _refold(d, agent, sm), n, sm.get("raw_offset", 0)
        except Exception:
            pass
    return _empty_summary(agent, d.name), 0, 0


def materialize_session(session_dir):
    d = pathlib.Path(session_dir)
    if not (d / "raw.jsonl").exists():
        return None
    lf = (d / ".materialize.lock").open("w")
    try:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                return json.loads((d / "summary.json").read_text())
            except Exception:
                return None
        return _materialize_locked(d)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()


def _materialize_locked(d):
    raw = d / "raw.jsonl"
    agent = d.parent.name

    summary, n_done, raw_offset = _load_state(d, agent)
    incremental = raw_offset > 0 and adapters.incremental(agent=agent)

    with raw.open("rb") as f:
        if incremental:
            f.seek(raw_offset)
        data = f.read()
    nl = data.rfind(b"\n")
    if nl < 0:
        return summary
    new_offset = (raw_offset if incremental else 0) + nl + 1
    store = raw_codec.load_store(d)
    records = [raw_codec.decode(r, store)
               for r in _parse_lines(data[:nl].decode("utf-8", "replace").splitlines())]
    if not incremental:
        summary, n_done = _empty_summary(agent, d.name), 0

    if not summary.get("name"):
        try:
            nm = adapters.session_name(records, agent=agent)
            if nm:
                summary["name"] = nm
        except Exception:
            pass
    if not summary.get("project"):
        try:
            pj = adapters.project_name(records, agent=agent)
            if pj:
                summary["project"] = pj
        except Exception:
            pass

    try:
        turns = [o for o in adapters.iter_turns(records, agent=agent) if o.get("usage")]
    except NotImplementedError:
        turns = []

    derived_path = d / "derived.jsonl"
    if not incremental:
        derived_path.write_text("")
    buf, turn = [], n_done

    def _flush(offset=None):
        nonlocal buf
        if buf:
            with derived_path.open("a") as f:
                f.write("\n".join(buf) + "\n")
            buf = []
        summary["n_turns"] = turn
        if offset is not None:
            summary["raw_offset"] = offset
        _atomic_write(d / "summary.json", json.dumps(summary, ensure_ascii=False))

    for obj in turns:
        model = obj.get("model") or ""
        try:
            evs = adapters.to_events(obj, turn, agent=agent, path=obj.get("path"))
            u = adapters.usage(obj, agent=agent, path=obj.get("path"))
            win = adapters.window(obj, agent=agent, path=obj.get("path"))
            tr = adapters.thinking_r(agent=agent, path=obj.get("path"))
            ia, oa = _exact_pins(obj, evs, model, agent, obj.get("path"))
            r = reconcile_turn(evs, u, model, anchors=ia, thinking_r=tr, output_anchors=oa)
        except NotImplementedError:
            break
        buf.append(json.dumps(_derived_line(turn, obj, model, win, u, r), ensure_ascii=False))
        _fold(summary, r, model)
        turn += 1
        if not incremental and len(buf) >= 64:
            _flush()
    _flush(offset=new_offset)
    try:
        _update_rollup(d, summary)
    except Exception:
        pass
    return summary


ROLLUP_VERSION = 2
MIN_BILL = 0.005
_AGG = ("bill", "tokens", "cached", "ci", "nt")


def _rollup_entry(summary, mtime):
    col = summary.get("columns", {})
    cached = col.get("cached", 0.0)
    ci = cached + col.get("rewrote", 0.0) + col.get("fresh", 0.0)
    return {"project": summary.get("project"), "name": summary.get("name"),
            "bill": summary.get("bill", 0.0), "nt": summary.get("n_turns", 0) or 0,
            "cached": cached, "ci": ci, "tokens": ci + col.get("output", 0.0), "mtime": mtime}


def _aggregate(data):
    projects, totals = {}, dict.fromkeys((*_AGG, "n_sessions"), 0.0)
    for b in (data.get("sessions") or {}).values():
        if b.get("bill", 0.0) < MIN_BILL:
            continue
        p = projects.setdefault(b.get("project") or "—", dict.fromkeys((*_AGG, "n_sessions"), 0.0))
        for k in _AGG:
            p[k] += b.get(k, 0.0)
            totals[k] += b.get(k, 0.0)
        p["n_sessions"] += 1
        totals["n_sessions"] += 1
    data["projects"], data["totals"] = projects, totals


def _rollup_locked(agent_dir, mutate):
    import fcntl
    agent_dir.mkdir(parents=True, exist_ok=True)
    rp = agent_dir / "_rollup.json"
    lk = agent_dir / "_rollup.lock"
    with lk.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = json.loads(rp.read_text())
        except Exception:
            data = None
        if not isinstance(data, dict) or data.get("version") != ROLLUP_VERSION:
            data = {"agent": agent_dir.name, "version": ROLLUP_VERSION, "sessions": {}}
        mutate(data)
        _aggregate(data)
        _atomic_write(rp, json.dumps(data, ensure_ascii=False))
        fcntl.flock(lf, fcntl.LOCK_UN)
    return data


def _update_rollup(session_dir, summary):
    d = pathlib.Path(session_dir)
    try:
        mt = (d / "summary.json").stat().st_mtime
    except OSError:
        mt = 0.0
    _rollup_locked(d.parent, lambda data: data["sessions"].__setitem__(d.name, _rollup_entry(summary, mt)))


def set_rollup_broken(agent_dir, names):
    _rollup_locked(pathlib.Path(agent_dir), lambda data: data.__setitem__("broken", sorted(names)))


def rebuild_rollup(agent_dir):
    agent_dir = pathlib.Path(agent_dir)

    def _fill(data):
        sessions = {}
        for sp in agent_dir.glob("*/summary.json"):
            try:
                sm = json.loads(sp.read_text())
                sessions[sp.parent.name] = _rollup_entry(sm, sp.stat().st_mtime)
            except Exception:
                pass
        data["sessions"] = sessions

    return _rollup_locked(agent_dir, _fill)


_NUM = ("tokens", "usd", "cached", "rewrote", "fresh", "output",
        "cached_usd", "rewrote_usd", "fresh_usd", "output_usd")


def _empty_summary(agent, sid):
    return {"session_id": sid, "agent": agent, "logic_version": LOGIC_VERSION,
            "n_turns": 0, "bill": 0.0,
            "columns": {"cached": 0.0, "rewrote": 0.0, "fresh": 0.0, "output": 0.0},
            "by_path": {}, "by_category": {}, "by_cat_tool": {}, "by_tool": {}, "by_mcp": {},
            "by_model": {}}


def _fold(summary, r, model):
    summary["bill"] += r["bill"]
    col = r["recon"]["ours"]["columns"]
    for k in ("cached", "rewrote", "fresh", "output"):
        summary["columns"][k] += col[k]
    for name, src in (("by_path", r["by_path"]), ("by_category", r["by_category"]),
                      ("by_cat_tool", r["by_cat_tool"]),
                      ("by_tool", r["by_tool"]), ("by_mcp", r["by_mcp"])):
        dst = summary[name]
        for k, v in src.items():
            kk = "|".join(map(str, k)) if isinstance(k, tuple) else k
            dd = dst.setdefault(kk, dict.fromkeys(_NUM, 0.0))
            for f in _NUM:
                dd[f] += v.get(f, 0.0)
    bm = summary["by_model"].setdefault(model, {"usd": 0.0, "turns": 0})
    bm["usd"] += r["bill"]
    bm["turns"] += 1
