from __future__ import annotations

import hashlib
import json
import pathlib

RAW = "raw.jsonl"
BLOCKS = "blocks.jsonl"

_FMT = "d1"
_REF_MSGS = "$m"
_REF_TOOLS = "$t"
_REF_SYS = "$s"
_REF_MK = "$mk"
_REF_MD = "$md"

_KEYFRAME_RATIO = 0.5
_KEYFRAME_CHAIN = 64


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _hash(block) -> str:
    return hashlib.sha256(_dump(block).encode("utf-8")).hexdigest()[:16]


def _is_delta(record) -> bool:
    return isinstance(record, dict) and record.get("_fmt") == _FMT


def _prefix(a, b) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _encode_messages(msgs, blocks, ctx):
    refs = []
    for m in msgs:
        h = _hash(m)
        blocks[h] = m
        refs.append(h)
    kf_refs = ctx.get("kf_refs") if ctx is not None else None
    keep = _prefix(kf_refs, refs) if kf_refs is not None else 0
    if (ctx is None or kf_refs is None
            or keep < _KEYFRAME_RATIO * len(refs)
            or ctx.get("chain", 0) >= _KEYFRAME_CHAIN):
        kh = _hash(refs)
        blocks[kh] = refs
        if ctx is not None:
            ctx["kf_refs"], ctx["kf_hash"], ctx["chain"] = refs, kh, 0
        return {_REF_MK: kh}
    ctx["chain"] = ctx.get("chain", 0) + 1
    return {_REF_MD: [ctx["kf_hash"], keep, refs[keep:]]}


def _decode_messages(slot, store):
    if _REF_MSGS in slot:
        refs = slot[_REF_MSGS]
    elif _REF_MK in slot:
        refs = store[slot[_REF_MK]]
    elif _REF_MD in slot:
        kh, keep, add = slot[_REF_MD]
        refs = store[kh][:keep] + list(add)
    else:
        return None
    return [store[h] for h in refs]


def encode(record, ctx=None):
    req = record.get("request") if isinstance(record, dict) else None
    if not isinstance(req, dict):
        return record, {}
    blocks: dict = {}
    dreq = dict(req)
    msgs = req.get("messages")
    if isinstance(msgs, list):
        dreq["messages"] = _encode_messages(msgs, blocks, ctx)
    tools = req.get("tools")
    if isinstance(tools, list):
        h = _hash(tools)
        blocks[h] = tools
        dreq["tools"] = {_REF_TOOLS: h}
    system = req.get("system")
    if system is not None and not isinstance(system, dict):
        h = _hash(system)
        blocks[h] = system
        dreq["system"] = {_REF_SYS: h}
    delta = dict(record)
    delta["request"] = dreq
    delta["_fmt"] = _FMT
    return delta, blocks


def decode(record, store):
    if not _is_delta(record):
        return record
    out = {k: v for k, v in record.items() if k != "_fmt"}
    req = dict(out.get("request") or {})
    m = req.get("messages")
    if isinstance(m, dict):
        dm = _decode_messages(m, store)
        if dm is not None:
            req["messages"] = dm
    t = req.get("tools")
    if isinstance(t, dict) and _REF_TOOLS in t:
        req["tools"] = store[t[_REF_TOOLS]]
    s = req.get("system")
    if isinstance(s, dict) and _REF_SYS in s:
        req["system"] = store[s[_REF_SYS]]
    out["request"] = req
    return out


def load_store(d: pathlib.Path) -> dict:
    p = pathlib.Path(d) / BLOCKS
    store: dict = {}
    if not p.exists():
        return store
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                h, block = json.loads(line)
            except Exception:
                continue
            store[h] = block
    return store


def load_hashes(d: pathlib.Path) -> set:
    p = pathlib.Path(d) / BLOCKS
    out: set = set()
    if not p.exists():
        return out
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.add(json.loads(line)[0])
            except Exception:
                continue
    return out


def append_record(d: pathlib.Path, record, *, store=None, seen=None, ctx=None) -> None:
    d = pathlib.Path(d)
    if seen is None:
        seen = set((store if store is not None else load_store(d)).keys())
    delta, blocks = encode(record, ctx)
    new = [(h, b) for h, b in blocks.items() if h not in seen]
    if new:
        with (d / BLOCKS).open("a") as f:
            for h, b in new:
                f.write(_dump([h, b]) + "\n")
                seen.add(h)
                if store is not None:
                    store[h] = b
    with (d / RAW).open("a") as f:
        f.write(_dump(delta) + "\n")


def iter_records(d: pathlib.Path):
    d = pathlib.Path(d)
    raw = d / RAW
    if not raw.exists():
        return
    store = load_store(d)
    with raw.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            yield decode(rec, store)


def latest_record(d: pathlib.Path):
    d = pathlib.Path(d)
    raw = d / RAW
    if not raw.exists():
        return None
    last = None
    with raw.open() as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if last is None:
        return None
    try:
        rec = json.loads(last)
    except Exception:
        return None
    return decode(rec, load_store(d) if _is_delta(rec) else {})
