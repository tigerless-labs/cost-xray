"""Exact Claude tokenization via Anthropic's own `count_tokens` API (design.md §6/§8).

Claude's tokenizer is private, so tiktoken o200k is only an *approximation* for Claude (it's
exact for Codex). The one exact route is Anthropic's `/v1/messages/count_tokens` endpoint. We
get exact **per-source** counts by differencing cumulative prefixes:

    A = count(messages)               → messages
    B = count(system, messages)       → +system
    C = count(system, tools, messages)→ full  (cross-checks against usage.input_tokens)
    ⇒ system = B−A, tools = C−B, static = C−A, messages = A

**Auth (two methods, both supported).** The proxy redacts the request's `authorization` /
`x-api-key` before disk, so the read layer has no credential of its own:
  1. **API key** — `COST_XRAY_ANTHROPIC_API_KEY` (or `ANTHROPIC_API_KEY`) → `x-api-key`.
  2. **OAuth** — the Claude Code login (`claude_login`) → `Bearer` + the oauth beta. The locator
     is config-dir-aware (`CLAUDE_CONFIG_DIR`), reads the file backend or the macOS Keychain, and
     withholds an expired token (with a clear notice). This is what Claude Code itself uses for
     `/context`, so a Max/Pro user needs no API key.
API key wins if both are present; if neither (or the login is expired), `exact_anchors` returns
`None` and the caller stays on tiktoken+calibration. Lazy + cached (network + rate limits).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
import urllib.request

from cost_xray import claude_login
from cost_xray.events import bucket_of

ENDPOINT = "https://api.anthropic.com/v1/messages/count_tokens"
VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"
_STUB = [{"role": "user", "content": "."}]
_CACHE: dict = {}


def _oauth_token():
    return claude_login.access_token()


def auth_headers(key=None, oauth=None):
    """Base request headers carrying auth — `x-api-key` for an API key, or `Bearer` + the
    oauth beta for the Claude Code OAuth token. `None` if neither is available."""
    key = key or os.environ.get("COST_XRAY_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return {"x-api-key": key, "anthropic-version": VERSION, "content-type": "application/json"}
    tok = oauth or _oauth_token()
    if tok:
        return {"authorization": f"Bearer {tok}", "anthropic-version": VERSION,
                "anthropic-beta": OAUTH_BETA, "content-type": "application/json"}
    return None


def _post(payload, headers):
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read()).get("input_tokens")


def _count(model, auth, *, system=None, tools=None, messages=None, _http=None):
    payload = {"model": model, "messages": messages or _STUB}
    if system is not None:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    return (_http or _post)(payload, dict(auth or {}))


def per_event_tokens(request, model, key=None, oauth=None, _http=None):
    """Exact tokens per static event — `system`, then **each tool** — via cumulative-prefix
    differencing against a stub message: each piece measured *in place*, so the diffs
    telescope. Plus a final `messages` marginal. Returns `[(label, tokens)]` whose sum equals
    `count(full)` minus the tiny stub baseline (≈ usage). Messages stays one piece on purpose
    — truncating it mid-conversation makes invalid sequences (a `tool_result` without its
    `tool_use`) that the API rejects. **~one API call per tool** — opt-in, small payloads
    except the final full count."""
    auth = {} if _http is not None else auth_headers(key, oauth)
    if auth is None or not isinstance(request, dict):
        return None
    system = request.get("system")
    tools = request.get("tools") or []
    messages = request.get("messages") or _STUB
    try:
        base = _count(model, auth, messages=_STUB, _http=_http)
        cur = _count(model, auth, system=system, messages=_STUB, _http=_http)
        out = [("system", cur - base)]
        for k in range(len(tools)):
            c = _count(model, auth, system=system, tools=tools[:k + 1], messages=_STUB, _http=_http)
            name = tools[k].get("name") or tools[k].get("type") or "?"
            out.append((f"tool:{name}", c - cur))
            cur = c
        full = _count(model, auth, system=system, tools=tools, messages=messages, _http=_http)
        out.append(("messages", full - cur))
    except Exception:
        return None
    return out


_OUT_TERM = {"type": "text", "text": "."}
_OUT_DIFFABLE = {"text", "thinking", "redacted_thinking", "output_text", "message", "reasoning"}


def per_output_event_tokens(blocks, model, key=None, oauth=None, _http=None):
    """Exact tokens per **output** block — **validation only**, never the hot path (verification.md).
    Claude's generated blocks aren't directly countable (`count_tokens` takes an *input* `messages`
    array), so we place them as a trailing assistant message and **cumulative-prefix difference**
    each `text`/`thinking` block in place (tail − head): `tokens(b_k) =
    count(stub + assistant[safe[:k] + TERM]) − count(stub + assistant[safe[:k-1] + TERM])`. The
    constant `TERM` text keeps every prefix valid (never ends in `thinking`) and cancels in the
    difference, so adjacent blocks never interfere. `tool_use` blocks are excluded (not isolable
    from their next-turn `tool_result`). Returns `[(bucket, tokens)]` (bucket via `events.bucket_of`)
    or `None` (no auth / error / no diffable blocks)."""
    auth = {} if _http is not None else auth_headers(key, oauth)
    if auth is None or not isinstance(blocks, list):
        return None
    safe = [({**b, "thinking": ""} if b.get("type") in ("thinking", "redacted_thinking")
             and "thinking" not in b else b)
            for b in blocks if isinstance(b, dict) and b.get("type") in _OUT_DIFFABLE]
    if not safe:
        return None

    def msgs(content):
        return [{"role": "user", "content": "."}, {"role": "assistant", "content": content + [_OUT_TERM]}]

    try:
        prev = _count(model, auth, messages=msgs([]), _http=_http)
        out = []
        for k in range(1, len(safe) + 1):
            cur = _count(model, auth, messages=msgs(safe[:k]), _http=_http)
            out.append((bucket_of(safe[k - 1].get("type")) or "text", cur - prev))
            prev = cur
    except Exception:
        return None
    return out


_OUT_BUCKET_TYPES = {
    "thinking": {"thinking", "redacted_thinking"},
    "text": {"text", "output_text"},
    "tool_io": {"tool_use", "server_tool_use"},
}


def _out_norm(blocks):
    """Output blocks with streamed thinking given a `thinking:""` (count_tokens requires the field)."""
    return [({**b, "thinking": ""} if b.get("type") in _OUT_BUCKET_TYPES["thinking"]
             and "thinking" not in b else b)
            for b in blocks if isinstance(b, dict)]


def _out_wrap(bs):
    """A **valid** `messages` array wrapping output blocks as an assistant turn — the shared shape
    behind every output differencer. Handles the three measured count_tokens constraints: a
    `tool_use` is paired with a synthetic `tool_result` (matched by id, trailing user message), a
    trailing `thinking` gets a `.` text terminator, and a stub user turn leads."""
    content, results = [], []
    for i, b in enumerate(bs):
        if b.get("type") in _OUT_BUCKET_TYPES["tool_io"]:
            tid = b.get("id") or f"toolu_synth_{i}"
            content.append({**b, "id": tid})
            results.append({"type": "tool_result", "tool_use_id": tid, "content": "."})
        else:
            content.append(b)
    if content and content[-1].get("type") in _OUT_BUCKET_TYPES["thinking"]:
        content = content + [{"type": "text", "text": "."}]
    msgs = [{"role": "user", "content": "."}, {"role": "assistant", "content": content}]
    if results:
        msgs.append({"role": "user", "content": results})
    return msgs


def per_output_bucket_tokens(blocks, model, key=None, oauth=None, _http=None):
    """Per-bucket **output** tokens (`thinking` / `text` / `tool_io`) by leave-one-out differencing
    on a valid assistant turn — the output analogue of `per_bucket_tokens`, **validation only**.
    Pins each bucket exactly so the output calibration can mirror the input (thinking via
    count_tokens, the rest tiktoken-proportional — verification.md). Returns `{bucket: tokens}` or
    `None`."""
    auth = {} if _http is not None else auth_headers(key, oauth)
    if auth is None or not isinstance(blocks, list):
        return None
    norm = _out_norm(blocks)
    if not norm:
        return None
    try:
        total = _count(model, auth, messages=_out_wrap(norm), _http=_http)
        out = {}
        for bucket, types in _OUT_BUCKET_TYPES.items():
            kept = [b for b in norm if b.get("type") not in types]
            marginal = total - _count(model, auth, messages=_out_wrap(kept), _http=_http)
            if marginal > 0:
                out[bucket] = marginal
    except Exception:
        return None
    return out


def input_thinking_tokens(messages, model, key=None, oauth=None, _http=None):
    """**Production** exact-mode helper: the input `thinking` size, the lean **2-call** diff
    `count(messages) − count(messages without thinking)` — the input analogue of
    `output_thinking_tokens`. Pins the input thinking bucket exactly in
    `reconcile_turn(anchors={"thinking": …})` instead of the `THINKING_R` estimate. Cached by
    content (the historical thinking blocks repeat across turns). Returns int, `0` (no thinking),
    or `None` (no auth / error)."""
    auth = {} if _http is not None else auth_headers(key, oauth)
    if auth is None or not isinstance(messages, list) or not messages:
        return None
    no_think = _without_thinking(messages)
    if no_think == messages:
        return 0
    h = "ithink:" + hashlib.sha1(json.dumps([model, messages], ensure_ascii=False,
                                            sort_keys=True, default=str).encode()).hexdigest()
    if h in _CACHE:
        return _CACHE[h]
    try:
        a = _count(model, auth, messages=messages, _http=_http)
        a_nt = _count(model, auth, messages=no_think, _http=_http)
    except Exception:
        return None
    val = max(0, a - a_nt) if (a is not None and a_nt is not None) else None
    _CACHE[h] = val
    return val


def output_thinking_tokens(blocks, model, key=None, oauth=None, _http=None):
    """**Production** exact-mode helper: just the output `thinking` size, the lean **2-call** diff
    `count(all) − count(without thinking)` on the valid wrapper (cheaper than the full
    `per_output_bucket_tokens`). Used to pin output thinking in `reconcile_turn(output_anchors=…)`
    so the displayed output text/thinking split is exact (verification.md). Returns int, `0` (no
    thinking), or `None` (no auth / error)."""
    auth = {} if _http is not None else auth_headers(key, oauth)
    if auth is None or not isinstance(blocks, list):
        return None
    norm = _out_norm(blocks)
    if not any(b.get("type") in _OUT_BUCKET_TYPES["thinking"] for b in norm):
        return 0
    no_think = [b for b in norm if b.get("type") not in _OUT_BUCKET_TYPES["thinking"]]
    try:
        total = _count(model, auth, messages=_out_wrap(norm), _http=_http)
        rest = _count(model, auth, messages=_out_wrap(no_think), _http=_http)
    except Exception:
        return None
    return max(0, total - rest)


_BUCKET_TYPES = {
    "thinking": {"thinking", "redacted_thinking"},
    "text": {"text", "input_text", "output_text"},
    "tool_io": {"tool_use", "server_tool_use", "tool_result", "web_search_tool_result"},
}
_PLACEHOLDER = {"type": "text", "text": "."}


def per_bucket_tokens(request, model, key=None, oauth=None, _http=None):
    """Per-bucket tokens within Messages by **leave-one-out differencing** in our own bucket
    classification — each bucket = `A − count(messages with that bucket's blocks removed)`,
    differenced against the full count A (not chained block-by-block), no proportional scaling.

    Two wire facts shape it: (1) removing a whole user-text turn empties a message and breaks
    role alternation (400), so emptied messages keep a `"."` placeholder (≈0 tokens); (2)
    `tool_use` and `tool_result` are paired — removing one orphans the other (400) — so they
    can only be removed together and are reported combined as `tool_io`. The per-bucket
    marginals miss the per-message framing that survives every single removal (~10%), so a
    `structure` row carries that honest remainder (`A − Σ marginals`); the rows sum to the
    exact Messages total A. ~4 calls over the full messages. Returns `{bucket: tokens}` /`None`.

    NOTE: this is the leave-one-out marginal — a bucket's *own* content. It is NOT the chained
    cumulative (telescoping) split; chaining would sum to A with no `structure` row but is the
    block-by-block prefix differencing we deliberately avoid here."""
    auth = {} if _http is not None else auth_headers(key, oauth)
    if auth is None or not isinstance(request, dict):
        return None
    messages = request.get("messages")
    if not messages:
        return None

    def without(types):
        out = []
        for m in messages:
            if not isinstance(m, dict):
                out.append(m)
                continue
            c = m.get("content")
            if isinstance(c, list):
                c = [b for b in c if not (isinstance(b, dict) and b.get("type") in types)] \
                    or [_PLACEHOLDER]
            elif isinstance(c, str) and "text" in types:
                c = [_PLACEHOLDER]
            out.append({**m, "content": c})
        return out

    try:
        total = _count(model, auth, messages=messages, _http=_http)
        out = {}
        for bucket, types in _BUCKET_TYPES.items():
            marginal = total - _count(model, auth, messages=without(types), _http=_http)
            if marginal > 0:
                out[bucket] = marginal
    except Exception:
        return None
    out["structure"] = max(0, total - sum(out.values()))
    return out


def _msg_block_bucket(t):
    for b, types in _BUCKET_TYPES.items():
        if t in types:
            return b
    return "text"


def _drop_blocks(messages, drop):
    """`messages` with the `(i, j)` coords in `drop` removed — a `.` placeholder if a message
    empties, a `.` terminator if a removal leaves a message ending in `thinking` (both invalid for
    count_tokens otherwise)."""
    out = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            out.append(m)
            continue
        c = m.get("content")
        if isinstance(c, str):
            out.append({**m, "content": [_PLACEHOLDER]} if (i, 0) in drop else m)
        elif isinstance(c, list):
            nc = [b for j, b in enumerate(c) if (i, j) not in drop]
            if not nc:
                nc = [_PLACEHOLDER]
            elif isinstance(nc[-1], dict) and nc[-1].get("type") in _BUCKET_TYPES["thinking"]:
                nc = nc + [_PLACEHOLDER]
            out.append({**m, "content": nc})
        else:
            out.append(m)
    return out


def per_message_event_tokens(request, model, key=None, oauth=None, _http=None):
    """**Validation only** (benchmark, verification.md): exact tokens for **each individual Messages
    block**, by leave-one-out differencing — `marginal(b) = A − count(messages without b)` against the
    full Messages count A, in our own bucketing. The finest input truth (per occurrence), one level
    below `per_bucket_tokens`. Same wire constraints: a `tool_use` and its `tool_result` are removed
    **together** (removing one orphans the other → 400) and reported as a single `tool_io` unit; an
    emptied message keeps a `.` placeholder; a removal leaving a trailing `thinking` gets a `.`
    terminator. Returns `[(coord, bucket, tokens)]`. For a `text`/`thinking` block
    `coord = ("msg", i, j)` (== `verify.ref_coord(ref)`). For a **`tool_io`** unit
    `coord = ("tool_io", <tool_id>)` — keyed by the tool id so both the `tool_use` and its
    `tool_result` `ours` events (they share `e["id"]`) map to the one truth row. ~one API call per
    block, so benchmark `--limit` bounds it. `None` on no-auth / error."""
    auth = {} if _http is not None else auth_headers(key, oauth)
    if auth is None or not isinstance(request, dict):
        return None
    messages = request.get("messages")
    if not messages:
        return None

    tool_types = _BUCKET_TYPES["tool_io"]
    blocks = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            blocks.append((i, 0, "text", None))
        elif isinstance(c, list):
            for j, b in enumerate(c):
                if isinstance(b, dict):
                    blocks.append((i, j, b.get("type"), b.get("id") or b.get("tool_use_id")))

    id_coords = {}
    for (i, j, t, pid) in blocks:
        if t in tool_types and pid:
            id_coords.setdefault(pid, []).append((i, j))
    units, seen = [], set()
    for (i, j, t, pid) in blocks:
        if t in tool_types and pid:
            if pid in seen:
                continue
            seen.add(pid)
            units.append((("tool_io", pid), "tool_io", set(id_coords[pid])))
        else:
            units.append((("msg", i, j), _msg_block_bucket(t), {(i, j)}))

    try:
        total = _count(model, auth, messages=messages, _http=_http)
        out = []
        for coord, bucket, drop in units:
            out.append((coord, bucket,
                        total - _count(model, auth, messages=_drop_blocks(messages, drop), _http=_http)))
    except Exception:
        return None
    return out


def _without_thinking(messages):
    """`messages` with every thinking block removed (emptied turns dropped — thinking is never a
    whole message, so this stays a valid sequence). For the thinking leave-one-out anchor."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = [b for b in c if not (isinstance(b, dict) and b.get("type") in _BUCKET_TYPES["thinking"])]
            if not c:
                continue
        out.append({**m, "content": c})
    return out


def exact_anchors(request, model, key=None, oauth=None, _http=None):
    """Exact per-source token counts for one Anthropic request via `count_tokens`, or `None`
    (no auth / error / non-dict). Returns `{system, tools, static, messages, thinking, total}`.
    `thinking` (leave-one-out `messages − no-thinking`) is what `reconcile_turn` uses to pin the
    thinking bucket exactly instead of the `THINKING_R` estimate. Costs one extra call only when
    thinking is present. Cached by request content; `_http` is the test seam."""
    auth = {} if _http is not None else auth_headers(key, oauth)
    if auth is None or not isinstance(request, dict):
        return None
    system = request.get("system")
    tools = request.get("tools") or []
    messages = request.get("messages") or _STUB
    h = hashlib.sha1(json.dumps([model, system, tools, messages], ensure_ascii=False,
                                sort_keys=True, default=str).encode()).hexdigest()
    if h in _CACHE:
        return _CACHE[h]
    try:
        a = _count(model, auth, messages=messages, _http=_http)
        b = _count(model, auth, system=system, messages=messages, _http=_http)
        c = _count(model, auth, system=system, tools=tools, messages=messages, _http=_http)
        no_think = _without_thinking(messages)
        a_nt = a if no_think == messages else _count(model, auth, messages=no_think, _http=_http)
    except Exception:
        _CACHE[h] = None
        return None
    if a is None or b is None or c is None or a_nt is None:
        _CACHE[h] = None
        return None
    out = {"system": max(0, b - a), "tools": max(0, c - b), "messages": a,
           "thinking": max(0, a - a_nt), "static": max(0, c - a), "total": c}
    _CACHE[h] = out
    return out


_TOOL_CACHE: dict = {}
_TOOL_TTL = 86400
_TOOL_STORE = pathlib.Path(os.path.expanduser("~/.cost-xray/tool_tokens.json"))


def _tool_store_load():
    try:
        return json.loads(_TOOL_STORE.read_text())
    except Exception:
        return {}


def _tool_store_save(store):
    try:
        _TOOL_STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _TOOL_STORE.with_name(_TOOL_STORE.name + ".tmp")
        tmp.write_text(json.dumps(store))
        tmp.replace(_TOOL_STORE)
    except Exception:
        pass


def tools_exact(tools, model, key=None, oauth=None, _http=None, now=None):
    """**Production** exact-mode helper: exact tokens per tool schema, **in context** — the
    cumulative-prefix marginals (`per_event_tokens`), aligned to `tools`. Must be in-context, not
    standalone: counting each tool *alone* charges the shared tool-framing N times (~32% over,
    measured live), and the marginals here sum to the real tools total instead.

    **Persistently cached** by the tools-set hash, with a **1-day TTL** (`tool_tokens.json` under
    `~/.cost-xray`): tool defs are static across a session and rarely change, so this is **one
    count_tokens computation per distinct tool set per day**, reused (across restarts) thereafter —
    and once cached the per-tool number is **always** the exact value, never re-derived from tiktoken.
    Group anchors can't fix the relative split between schemas (verification.md), so per-tool
    differencing is the only exact route. `now` is a test seam. Returns `[tokens]` aligned to
    `tools`, or `None` (no auth / error)."""
    if not isinstance(tools, list) or not tools:
        return None
    ck = f"{model}|" + hashlib.sha1(json.dumps(tools, ensure_ascii=False, sort_keys=True,
                                               default=str).encode()).hexdigest()
    if ck in _TOOL_CACHE:
        return _TOOL_CACHE[ck]
    t = time.time() if now is None else now
    store = _tool_store_load()
    ent = store.get(ck)
    if isinstance(ent, dict) and ent.get("vals") is not None and (t - ent.get("ts", 0)) < _TOOL_TTL:
        _TOOL_CACHE[ck] = ent["vals"]
        return ent["vals"]
    pe = per_event_tokens({"tools": tools, "messages": _STUB}, model, key, oauth, _http=_http)
    vals = [tok for lbl, tok in pe if lbl.startswith("tool:")] if pe else None
    out = vals if (vals is not None and len(vals) == len(tools)) else None
    _TOOL_CACHE[ck] = out
    if out is not None:
        store[ck] = {"vals": out, "ts": t}
        _tool_store_save(store)
    return out
