"""Data + formatting layer for the TUI (the interactive app is `tui_app.py`).

Session discovery, the materialized-`summary` reader (with a non-blocking background-materialize
kick), the latest-`derived`-line reader, and the small render helpers (`_h` / `_bar` / `_ROW` /
`_split`). The Textual app in `tui_app.py` imports these; **this module renders nothing itself**
— there is exactly one TUI (`tui_app`).
"""
from __future__ import annotations

import json
import os
import pathlib
import threading

from rich.text import Text

from cost_xray import raw_codec
from cost_xray.analyze import analyze
from cost_xray.materialize import materialize_session

ROOT = pathlib.Path(os.path.expanduser("~/.cost-xray/sessions"))
PIN = os.environ.get("COST_XRAY_SESSION")

_ROW = {"system": "System prompt", "schema": "Tool schemas", "text": "text",
        "thinking": "thinking", "tool_use": "tool_use", "tool_result": "tool_result"}



def _h(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def _bar(frac: float, width: int = 28, color: str = "white") -> Text:
    filled = max(0, min(width, round(frac * width)))
    return Text("█" * filled + "·" * (width - filled), style=color)



def _sessions() -> list[dict]:
    """Return all sessions, newest activity first."""
    out = []
    if not ROOT.exists():
        return out
    for d in ROOT.glob("*/*"):
        if not d.is_dir():
            continue
        times = []
        for fn in ("raw.jsonl", "summary.json", "meta.json"):
            p = d / fn
            if p.exists():
                try:
                    times.append(p.stat().st_mtime)
                except OSError:
                    pass
        if not times:
            continue
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text())
        except Exception:
            pass
        out.append({"dir": d, "sid": d.name, "agent": d.parent.name,
                    "mtime": max(times), "meta": meta})
    out.sort(key=lambda s: -s["mtime"])
    return out


def _latest_request(d: pathlib.Path):
    """Raw request body of the last completed turn (legacy analyze path) — via the raw codec, so a
    deduped delta record is reconstructed and a legacy whole-body record passes through."""
    try:
        rec = raw_codec.latest_record(d)
        if rec:
            return rec.get("request")
    except Exception:
        pass
    return None


def _report_for(d: pathlib.Path) -> dict | None:
    """Legacy single-request decomposition (analyze()). Kept for the read-time path and
    its tests; the live panels use the event pipeline below."""
    req = _latest_request(d)
    if req is not None:
        try:
            return analyze(req)
        except Exception:
            return None
    leg = d / "latest.json"
    if leg.exists():
        try:
            return json.loads(leg.read_text())
        except Exception:
            return None
    return None



_SUMMARY_CACHE: dict = {}
_MAT_THREADS: dict = {}


def _safe_materialize(d: pathlib.Path) -> None:
    try:
        materialize_session(d)
    except Exception:
        pass


def _ensure_fresh(d: pathlib.Path) -> None:
    """Kick a background materialize (in THIS process — the TUI worker, NEVER the proxy;
    invariant #1) when raw.jsonl is newer than summary.json. Deduped per dir, non-blocking —
    the render loop never waits on tokenization. The materializer is incremental, so once warm
    this is ~instant."""
    raw, sp = d / "raw.jsonl", d / "summary.json"
    if not raw.exists():
        return
    try:
        raw_mt = raw.stat().st_mtime
        sum_mt = sp.stat().st_mtime if sp.exists() else 0.0
    except OSError:
        return
    if raw_mt <= sum_mt:
        return
    t = _MAT_THREADS.get(str(d))
    if t is not None and t.is_alive():
        return
    th = threading.Thread(target=_safe_materialize, args=(d,),
                          name="cost-xray-materialize", daemon=True)
    _MAT_THREADS[str(d)] = th
    th.start()


_ROLLUP_CACHE: dict = {}


def _rollup(agent_dir: pathlib.Path):
    """The per-agent **basic rollup** (`<agent>/_rollup.json`) — one read gives every session's
    basics (project / name / bill / tokens / hit inputs), so Home builds its agent→project→session
    tree without opening N summaries. Regenerable: rebuilt when missing/stale or when a session dir
    isn't yet indexed. The materializer keeps live entries fresh on each run. Cached by file mtime."""
    from cost_xray.materialize import rebuild_rollup
    rp = agent_dir / "_rollup.json"
    have = {x.name for x in agent_dir.iterdir()
            if x.is_dir() and (x / "summary.json").exists()} if agent_dir.exists() else set()
    data = None
    try:
        mt = rp.stat().st_mtime if rp.exists() else 0.0
        hit = _ROLLUP_CACHE.get(str(agent_dir))
        if hit and hit[0] == mt:
            data = hit[1]
        elif rp.exists():
            data = json.loads(rp.read_text())
            _ROLLUP_CACHE[str(agent_dir)] = (mt, data)
    except Exception:
        data = None
    if not isinstance(data, dict) or set(data.get("sessions", {})) != have:
        try:
            data = rebuild_rollup(agent_dir)
            _ROLLUP_CACHE[str(agent_dir)] = (rp.stat().st_mtime, data)
        except Exception:
            data = data if isinstance(data, dict) else {"sessions": {}}
    return data


def _summary(d: pathlib.Path):
    """Read the materialized summary.json — **never materializes in the render path**. If
    raw.jsonl is newer, a background worker is kicked so the NEXT read is fresh; the render
    itself only reads already-computed numbers. Cached by summary.json mtime."""
    _ensure_fresh(d)
    sp = d / "summary.json"
    if not sp.exists():
        return None
    try:
        mt = sp.stat().st_mtime
    except OSError:
        return None
    hit = _SUMMARY_CACHE.get(str(d))
    if hit and hit[0] == mt:
        return hit[1]
    try:
        s = json.loads(sp.read_text())
    except Exception:
        s = None
    _SUMMARY_CACHE[str(d)] = (mt, s)
    return s


def _latest_derived(d: pathlib.Path):
    """The latest derived turn line (this turn's **already-calibrated** context) — read, not
    computed. Returns {turn, window, usage, events:[...]} or None. Tail-reads the file so it
    stays cheap on a large derived."""
    p = d / "derived.jsonl"
    if not p.exists():
        return None
    try:
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 1_048_576))
            data = f.read()
        for ln in reversed(data.split(b"\n")):
            ln = ln.strip()
            if not ln:
                continue
            try:
                return json.loads(ln)
            except Exception:
                continue
        return None
    except Exception:
        return None


def _split(key):
    z, s, b = key.split("|")
    return z, (None if s == "None" else s), b
