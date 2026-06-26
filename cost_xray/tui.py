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
    try:
        rec = raw_codec.latest_record(d)
        if rec:
            return rec.get("request")
    except Exception:
        pass
    return None


def _report_for(d: pathlib.Path) -> dict | None:
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
_MAT_CAP = 2
_MAT_LOCK = threading.Lock()


def _safe_materialize(d: pathlib.Path) -> None:
    try:
        materialize_session(d)
    except Exception:
        pass


def _ensure_fresh(d: pathlib.Path) -> None:
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
    with _MAT_LOCK:
        t = _MAT_THREADS.get(str(d))
        if t is not None and t.is_alive():
            return
        if sum(1 for x in _MAT_THREADS.values() if x.is_alive()) >= _MAT_CAP:
            return
        th = threading.Thread(target=_safe_materialize, args=(d,),
                              name="cost-xray-materialize", daemon=True)
        _MAT_THREADS[str(d)] = th
        th.start()


_ROLLUP_CACHE: dict = {}


def _rollup(agent_dir: pathlib.Path):
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


_MAIN_CACHE: dict = {}
_MAIN_FRAC = 0.5
_MAIN_TAIL_BYTES = 8_000_000


def _tail_records(p: pathlib.Path, max_bytes: int = _MAIN_TAIL_BYTES):
    with p.open("rb") as f:
        f.seek(0, 2)
        start = max(0, f.tell() - max_bytes)
        f.seek(start)
        data = f.read()
    parts = data.split(b"\n")
    if start:
        parts = parts[1:]
    out = []
    for ln in parts:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _input_static_tokens(rec, is_static) -> int:
    return sum(e.get("tokens", 0) for e in rec.get("events", [])
               if e.get("zone") == "input" and is_static(e))


def _pick_main_line(recs, is_static):
    if not recs:
        return None
    scored = [(r, _input_static_tokens(r, is_static)) for r in recs]
    scaffold = max(s for _, s in scored)
    if scaffold <= 0:
        return recs[-1]
    thresh = _MAIN_FRAC * scaffold
    for r, s in reversed(scored):
        if s >= thresh:
            return r
    return recs[-1]


def _latest_main_derived(d, is_static):
    p = pathlib.Path(d) / "derived.jsonl"
    try:
        st = p.stat()
    except OSError:
        return None
    key = str(p)
    c = _MAIN_CACHE.get(key)
    if c is not None and c[0] == st.st_mtime_ns and c[1] == st.st_size:
        return c[2]
    line = _pick_main_line(_tail_records(p), is_static)
    _MAIN_CACHE[key] = (st.st_mtime_ns, st.st_size, line)
    return line


def _split(key):
    z, s, b = key.split("|")
    return z, (None if s == "None" else s), b
