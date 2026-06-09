"""Raw codec — the one seam the dedup storage format touches (design/read-layer.md).

A long session re-sends its whole history every turn, so storing each turn's full body verbatim is
quadratic. The codec splits a turn into content-addressed **blocks** (each `messages` element, plus
the `tools` array and `system` whole) and a lightweight **delta record** that keeps everything else
verbatim and replaces those values with refs into a per-session **block store**. Reconstruction
resolves the refs back, byte-for-byte, so every downstream consumer sees the same per-turn record as
before. Legacy whole-body records (no delta marker) pass through unchanged, so old and new sessions
coexist.

The encode/decode pair is pure (record <-> (delta, new blocks)); the dir-level helpers own the
block store and the append-only delta log.
"""
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
    """The message list's reference slot. Each message is a block; the ordered ref list is stored as a
    keyframe (the whole list as one content-addressed block) or a delta against the most recent
    keyframe (kept prefix + new tail). A re-keyframe fires when the history is rewritten (prefix
    collapses) or the chain would grow unbounded. `ctx` (kept by the writer across turns) holds the
    current keyframe's hash/refs + chain length; `ctx is None` makes every list a self-contained
    keyframe."""
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
    """Split a full record into a (delta_record, {hash: block}) pair. The delta keeps every field
    verbatim except `request.messages` / `request.tools` / `request.system`, which become refs; the
    returned blocks are the unique pieces to add to the store. `ctx` carries the writer's keyframe
    state across turns so the message ref list is delta-encoded (omit it for a self-contained record).
    A record with no dict `request` (e.g. a Codex WS frame) is returned unchanged with no blocks."""
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
    """Reconstruct a full record from a delta record + the block `store` (hash -> block). A
    non-delta (legacy) record is returned unchanged. Reconstruction is stateless per record — a delta
    message slot resolves from its keyframe block alone, no forward replay — so the result is identical
    to the original record and `_dump(decode(...))` equals the original raw line byte-for-byte."""
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
    """The session's block store (hash -> block). Empty when no sidecar exists (a legacy-only or
    fresh session)."""
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
    """Just the hashes already in the block store — what a writer needs to skip re-appending a block,
    without holding the blocks themselves in memory. Empty for a fresh/legacy session."""
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
    """Append one full record to a session dir in deduped form: new blocks to the block store, the
    delta to the raw log. `seen` (a hash set), `store`, and `ctx` (the writer's keyframe state) may be
    carried across calls by a writer so it never re-reads the sidecar and the message ref list is
    delta-encoded; when omitted the store is loaded from disk once and each record is self-contained."""
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
    """Stream every turn's full record from a session dir, reconstructing deltas via the block store
    and passing legacy whole-body records through. One streaming pass over the raw log."""
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
    """The last turn's full record, or None. Tails the raw log so it stays cheap on a large session;
    the block store is consulted only for the one line it reconstructs."""
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
