"""Materializer: `raw.jsonl` → `derived.jsonl` + `summary.json` (design.md §1/§4).

Runs in a **separate consumer process** (the TUI's background worker), never in the proxy —
tokenization/calibration must not steal GIL/CPU from live API relay (invariant #1).

Per completed turn, in this fixed order (design.md §4):
  1. adapter → canonical events (raw tiktoken sizes)
  2. `reconcile_turn` → **calibrate every event** (thinking via count_tokens/THINKING_R, rest
     tiktoken-proportional to `usage`) + cut at the cache boundary → cached/rewrote/fresh + $
  3. **append** one self-contained `derived` line carrying the *calibrated* events
  4. fold it into the cumulative `summary` (a running sum — never recalibrated)

Incremental: only new completed turns are processed and appended; `summary` is loaded and
folded onto. A logic-version bump / any inconsistency falls back to a full rebuild from `raw`.
Downstream (`summary`, TUI) only ever sums / groups these already-calibrated numbers.
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib

from cost_xray import adapters, count_tokens, raw_codec
from cost_xray.adapters import anthropic as _anthropic
from cost_xray.classify import _WRITE_1H_FACTOR, reconcile_turn, rollup
from cost_xray.cost import rates

LOGIC_VERSION = 9   # v3: output thinking sig; v4: per-cat cache $; v5: Messages tool I/O = MCP/system tool use+output; v6: by_cat_tool (drill server/tool from summary); v7: Output tool I/O merged + MCP/system split; v8: exact mode — output thinking pinned (Codex reasoning_tokens / Claude count_tokens) + per-tool exact (cached) when count_tokens auth present; v9: skill attribution — ads carved from system-role messages too, injected SKILL.md body → per-skill "Skill loads" category, Skill tool call no longer per-skill (changes the derived `skill` field → full rebuild, not refold)


def _exact_pins(obj, evs, model, agent, path):
    """Exact-mode pins for one turn (design/verification.md) — gated on auth, cached, and
    **fail-open**: any error leaves things unpinned (today's tiktoken+`THINKING_R` path), so a
    no-auth user makes **zero** network calls and sees identical output.

    Returns `(input_anchors, output_anchors)` — pinning the input & output thinking buckets — and
    sets `exact` on tool-schema events in place (pins the per-tool number). Output thinking is
    **free** from the wire when the agent exposes it (Codex `reasoning_tokens`); Claude uses
    count_tokens (input thinking 2 calls, output thinking 2 calls, both content-cached). Input
    thinking + per-tool exact are count_tokens-only and Anthropic-only (Codex is already o200k-exact
    everywhere); the per-tool result is persistently cached (static tools → ~one call/set/day)."""
    in_anchors = out_anchors = None
    use_ct = (adapters.adapter_for(agent=agent, path=path) is _anthropic
              and count_tokens.auth_headers() is not None)
    try:
        ot = adapters.output_thinking(obj, agent=agent, path=path)      # free for Codex
        if ot is None and use_ct:
            blocks = adapters.response_blocks(obj, agent=agent, path=path)
            if blocks:
                ot = count_tokens.output_thinking_tokens(blocks, model)
        if ot is not None:
            out_anchors = {"thinking": ot}
    except Exception:
        pass
    if use_ct:                                                          # input thinking via count_tokens (Anthropic)
        try:
            msgs = (obj.get("request") or {}).get("messages")           # use_ct ⇒ Anthropic record
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
                vals = count_tokens.tools_exact(tools, model)   # one cached call for the whole set
                if vals and len(vals) == len(tool_evs):
                    for e, v in zip(tool_evs, vals, strict=True):
                        e["exact"] = v
        except Exception:
            pass
    return in_anchors, out_anchors


def _atomic_write(path, text):
    """Write via a temp file + atomic rename, so a concurrent reader (the TUI) never sees a
    half-written summary — it gets either the previous complete one or the new complete one,
    never a partial/blank file."""
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
    """Project one reconciled (calibrated) event to the fields the read layer needs.
    `tokens` is the **calibrated** size; cache split + $ are already computed; `ref`/`id`/
    `type`/`hash` are kept so drill-down can fetch the real content back out of `raw`."""
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
        "events": [_derived_event(e) for e in r["events"]],   # calibrated, self-contained
    }


# A summary at >= this version can be **re-folded from `derived`** (no re-tokenize) when only the
# fold/`category` logic changed. The per-event field *schema* has been stable since v5, but v9
# changed event *content* (the `skill` field: ads carved from messages, SKILL.md body tagged,
# Skill tool call un-tagged) — refolding stale `derived` would carry old skill labels, so pre-v9
# must full-rebuild from `raw`.
MIN_REFOLD = 9


def _enrich_from_derived(line):
    """Reconstruct calibrated, $-split events from one stored `derived` line — enough for
    `classify.rollup`, the inverse of `_derived_event`. The per-event cache/output $ split is
    recomputed from the stored **token** split × the model rates (no tokenization); `usd` is kept
    as stored (unrounded). Powers the refold-from-derived fast path."""
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
    """Rebuild `summary` from the existing `derived.jsonl` (re-aggregate, **no tokenization**) so a
    fold-only logic bump (e.g. a `category` relabel) upgrades cheaply instead of re-reading `raw`.
    Reuses the old `raw_offset` and persists, so the normal incremental loop then just continues
    from where `derived` left off."""
    summary = _empty_summary(agent, d.name)
    for k in ("name", "project"):       # carry session label + project across a fold-only refold
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
    _atomic_write(d / "summary.json", json.dumps(summary, ensure_ascii=False))   # persist even if no new turns
    return summary


def _load_state(d, agent):
    """(summary, n_done, raw_offset) to resume from, or (empty, 0, 0) to rebuild — on missing
    files, a logic-version bump, or any summary/derived inconsistency. A **fold-only** bump (derived
    still schema-compatible, >= MIN_REFOLD, incremental agent) re-folds from `derived` instead of
    re-tokenizing `raw`."""
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
    """(Re)build a session's derived/summary under a **non-blocking per-session lock**, so the
    daemon, a sweep, and any residual TUI kick never materialize the same session at once. On
    contention return the existing summary (or None) without re-processing — the holder is already
    doing the work. Returns the summary."""
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
    """The materialize body (lock already held). Incremental for record-per-turn agents
    (`adapters.incremental`) — only the **new raw bytes** are parsed (byte offset tracked in
    `summary`); else a full read. A logic-version bump or any inconsistency forces a full rebuild.
    `raw` is append-only, so the offset is always a line boundary; we advance only to the last
    complete `\\n`."""
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
        return summary                              # no complete (new) line yet
    new_offset = (raw_offset if incremental else 0) + nl + 1
    store = raw_codec.load_store(d)                  # dedup: resolve delta records; {} for legacy
    records = [raw_codec.decode(r, store)
               for r in _parse_lines(data[:nl].decode("utf-8", "replace").splitlines())]
    if not incremental:                             # full rebuild
        summary, n_done = _empty_summary(agent, d.name), 0

    if not summary.get("name"):                     # session label = first user message (once)
        try:
            nm = adapters.session_name(records, agent=agent)
            if nm:
                summary["name"] = nm
        except Exception:
            pass
    if not summary.get("project"):                  # project = full cwd path (once)
        try:
            pj = adapters.project_name(records, agent=agent)
            if pj:
                summary["project"] = pj
        except Exception:
            pass

    try:                                            # incremental → only the new turns
        turns = [o for o in adapters.iter_turns(records, agent=agent) if o.get("usage")]
    except NotImplementedError:
        turns = []

    derived_path = d / "derived.jsonl"
    if not incremental:
        derived_path.write_text("")                 # truncate for a full rebuild
    buf, turn = [], n_done

    def _flush(offset=None):
        """Append buffered derived lines + write summary atomically. During a full rebuild we
        flush periodically (offset=None, raw_offset left uncommitted so an interrupted rebuild
        re-runs) so the screen fills progressively — no long blank. The final flush commits the
        raw_offset that enables the next incremental run."""
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
            ia, oa = _exact_pins(obj, evs, model, agent, obj.get("path"))   # exact mode (gated/fail-open)
            r = reconcile_turn(evs, u, model, anchors=ia, thinking_r=tr, output_anchors=oa)
        except NotImplementedError:
            break
        buf.append(json.dumps(_derived_line(turn, obj, model, win, u, r), ensure_ascii=False))
        _fold(summary, r, model)
        turn += 1
        if not incremental and len(buf) >= 64:      # progressive fill on the slow full rebuild
            _flush()
    _flush(offset=new_offset)                       # final: commit raw_offset
    try:
        _update_rollup(d, summary)                  # keep the per-agent basic-rollup cache fresh
    except Exception:
        pass
    return summary


# --- per-agent rollup cache (basic totals only; no details) -------------------------
# A `sessions/<agent>/_rollup.json` index of every session's *basic* fields, so the Home screen
# reads ONE file per agent instead of N per-session summaries. It's a regenerable cache (like
# `summary.json`): the materializer refreshes one session's entry after each run; a stale/missing
# rollup is rebuilt from the summaries. **Basics only** — bill / tokens / hit inputs / project /
# name — never the per-category details (those stay in `summary.json`, read only on drill-in).
ROLLUP_VERSION = 2          # v2: precomputed per-project + agent totals
MIN_BILL = 0.005            # a session below this ($0.00) is a probe — excluded from rollup totals
_AGG = ("bill", "tokens", "cached", "ci", "nt")


def _rollup_entry(summary, mtime):
    col = summary.get("columns", {})
    cached = col.get("cached", 0.0)
    ci = cached + col.get("rewrote", 0.0) + col.get("fresh", 0.0)
    return {"project": summary.get("project"), "name": summary.get("name"),
            "bill": summary.get("bill", 0.0), "nt": summary.get("n_turns", 0) or 0,
            "cached": cached, "ci": ci, "tokens": ci + col.get("output", 0.0), "mtime": mtime}


def _aggregate(data):
    """(Re)compute the per-project + agent **totals** from `data['sessions']` — the cached numbers
    the Home tree reads without summing. Recomputed on **every** rollup write, so each `derived`
    update refreshes its project's (and the agent's) totals + cache. $0-probe sessions are excluded
    so the counts match the Home filter. Project key `'—'` = sessions with no detected cwd."""
    projects, totals = {}, dict.fromkeys((*_AGG, "n_sessions"), 0.0)
    for b in (data.get("sessions") or {}).values():
        if b.get("bill", 0.0) < MIN_BILL:
            continue                                   # $0 / 0-turn probe → not real spend
        p = projects.setdefault(b.get("project") or "—", dict.fromkeys((*_AGG, "n_sessions"), 0.0))
        for k in _AGG:
            p[k] += b.get(k, 0.0)
            totals[k] += b.get(k, 0.0)
        p["n_sessions"] += 1
        totals["n_sessions"] += 1
    data["projects"], data["totals"] = projects, totals


def _rollup_locked(agent_dir, mutate):
    """Read-modify-write `<agent>/_rollup.json` under an flock, so concurrent per-session
    materializes (and a reader's rebuild) never clobber each other. `mutate(data)` edits sessions
    in place; the project/agent totals are then recomputed and written together."""
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
        _aggregate(data)                               # refresh project + agent totals on every write
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


def rebuild_rollup(agent_dir):
    """Rebuild a per-agent rollup by scanning every session's `summary.json` (the one O(sessions)
    pass; then reads are O(1) per agent). Returns the rollup dict."""
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
        "cached_usd", "rewrote_usd", "fresh_usd", "output_usd")   # per-cache-state $ per category


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
