"""mitmproxy addon: capture coding-agent LLM traffic + decompose the context.

Reuses mitmproxy as the capture plumbing (reverse-proxy mode = no CA cert):

    mitmdump --mode reverse:https://api.anthropic.com -p 8788 -s cost_xray/addon.py
    ANTHROPIC_BASE_URL=http://127.0.0.1:8788 claude

Capture approach adapted from llm-interceptor (MIT, https://github.com/chouzz/llm-interceptor):
  - detect streaming via `content-type: text/event-stream`
  - **buffer the response** (mitmproxy default) and parse the SSE body into
    structured events — reliable, unlike teeing the live stream
  - redact secrets in headers and body
Buffering means the agent receives each complete response (it waits for the full
message before acting anyway); mitmproxy handles the HTTP correctly so agentic
tool loops are not broken.

Capture is decoupled from analysis: this addon only records raw bytes (keeping the
proxy hot path thin — no tokenization blocking request forwarding). Decomposition
(`analyze.py`) runs in the TUI at read time, so analysis can improve without
re-capturing, and old sessions get re-analyzed with the latest logic.

Storage (layered like the SWE-bench `logs/run_evaluation/<run>/<agent>/<instance>/`
tree) — keyed by the real agent + session id, so a long-running daemon keeps each
coding session in its own directory:

  ~/.cost-xray/sessions/<agent>/<session_id>/
      meta.json     agent / model / first_seen / last_seen / n_turns
      raw.jsonl     HTTP: one record per completed turn. Codex WebSocket: one frame per
                    line, appended in realtime. Append-only; never rewritten.

The session id comes straight from Claude Code's `X-Claude-Code-Session-Id`
header (same UUID as ~/.claude/projects/.../<uuid>.jsonl), with fallbacks.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import pathlib
import queue
import threading

try:                                  # package context (tests / `-m`)
    from cost_xray import raw_codec
except ImportError:                   # mitmproxy loads addon.py by path → cost_xray/ is sys.path[0]
    import raw_codec

OUT = pathlib.Path(os.path.expanduser("~/.cost-xray"))
SESSIONS = OUT / "sessions"
SESSIONS.mkdir(parents=True, exist_ok=True)
_WRITE_Q: queue.Queue = queue.Queue()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()

MATCH = ("/v1/messages", "/responses", "/chat/completions")
SECRET_HEADERS = {"authorization", "x-api-key", "anthropic-api-key", "openai-api-key",
                  "api-key", "cookie", "set-cookie", "x-goog-api-key", "proxy-authorization"}
SECRET_BODY_KEYS = ("api_key", "apikey", "authorization", "token", "secret", "password")


def _matched(flow) -> bool:
    return any(p in flow.request.path for p in MATCH)


def _redact_headers(headers) -> dict:
    return {k: ("<redacted>" if k.lower() in SECRET_HEADERS else v) for k, v in headers.items()}


def _redact_body(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k.lower() in SECRET_BODY_KEYS else _redact_body(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_body(v) for v in obj]
    return obj


def _agent_of(ua: str) -> str:
    ua = (ua or "").lower()
    if "claude" in ua:
        return "claude"
    if "codex" in ua:
        return "codex"
    if "cursor" in ua:
        return "cursor"
    return "unknown"


def _agent_for(flow) -> str:
    """Agent from User-Agent, with path/host fallback (Codex's UA is unreliable)."""
    a = _agent_of(flow.request.headers.get("user-agent", ""))
    if a != "unknown":
        return a
    path = flow.request.path or ""
    host = (flow.request.host or "").lower()
    if "/backend-api/codex" in path or "chatgpt.com" in host:
        return "codex"
    return "unknown"


def _session_id(flow, body) -> str:
    """Best-effort stable session id for grouping a coding session's turns.

    Claude Code sends `X-Claude-Code-Session-Id` (== ~/.claude/projects/.../<uuid>.jsonl).
    Fall back to the session_id embedded in metadata.user_id, then the connection id.
    """
    h = flow.request.headers
    sid = h.get("x-claude-code-session-id") or h.get("x-session-id")
    if sid:
        return sid
    if isinstance(body, dict):
        md = body.get("metadata") or {}
        uid = md.get("user_id")
        if isinstance(uid, str):
            try:
                sid = json.loads(uid).get("session_id")
            except Exception:
                sid = None
        if sid:
            return sid
        pck = body.get("prompt_cache_key")
        if pck:
            return str(pck)
        cm = body.get("client_metadata")
        if isinstance(cm, dict):
            try:
                tm = json.loads(cm.get("x-codex-turn-metadata") or "{}")
                if tm.get("session_id"):
                    return str(tm["session_id"])
            except Exception:
                pass
    cid = getattr(getattr(flow, "client_conn", None), "id", None)
    return f"conn-{str(cid)[:8]}" if cid else "unknown"


def _session_dir(flow, body) -> pathlib.Path:
    agent = _agent_for(flow)
    d = SESSIONS / agent / _session_id(flow, body)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_info(flow, body, ts: float) -> dict:
    return {
        "session_id": None,
        "agent": None,
        "ts": ts,
        "model": body.get("model") if isinstance(body, dict) else None,
        "user_agent": flow.request.headers.get("user-agent", ""),
        "host": flow.request.host,
    }


def _update_meta(d: pathlib.Path, flow, body, ts: float) -> None:
    _update_meta_info(d, _meta_info(flow, body, ts))


def _update_meta_info(d: pathlib.Path, info: dict) -> None:
    path = d / "meta.json"
    try:
        meta = json.loads(path.read_text())
    except Exception:
        meta = {}
    meta.setdefault("session_id", d.name)
    meta.setdefault("agent", d.parent.name)
    ts = info.get("ts")
    meta.setdefault("first_seen", ts)
    meta["last_seen"] = ts
    meta["n_turns"] = meta.get("n_turns", 0) + 1
    if info.get("model"):
        meta["model"] = info["model"]
    meta["user_agent"] = info.get("user_agent", "")
    meta["host"] = info.get("host", "")
    try:
        path.write_text(json.dumps(meta))
    except Exception:
        pass


def _ensure_worker() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        t = threading.Thread(target=_writer_loop, name="cost-xray-writer", daemon=True)
        t.start()
        _WORKER_STARTED = True


def _enqueue(job) -> None:
    _ensure_worker()
    _WRITE_Q.put(job)


_CONSUMER_CONN = None
_CONSUMER_LOCK = threading.Lock()


def _ensure_consumer():
    """Lazily start the **one warm materializer consumer** (a separate process — invariant #1) and
    return our end of its signal pipe. Started via the `spawn` context (a fresh interpreter, never a
    fork of this multithreaded proxy), so it imports the tokenizer **once** and stays warm. It is a
    daemon child bound to this proxy: when the proxy dies, the pipe reaches EOF and the consumer exits.
    Restarted transparently if it ever died."""
    global _CONSUMER_CONN
    with _CONSUMER_LOCK:
        proc = getattr(_ensure_consumer, "_proc", None)
        if _CONSUMER_CONN is not None and proc is not None and proc.is_alive():
            return _CONSUMER_CONN
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe()
        proc = ctx.Process(target=_consume_entry, args=(child_conn,),
                           name="cost-xray-materializer", daemon=True)
        proc.start()
        child_conn.close()                               # the child holds its own copy
        _ensure_consumer._proc = proc
        _CONSUMER_CONN = parent_conn
        return _CONSUMER_CONN


def _consume_entry(conn) -> None:
    """Child entrypoint: run the warm sweep loop. `consume` is imported **here, in the child**, so the
    proxy process never pulls in the tokenizer (invariant #1). The dual import mirrors this module's
    top — package context (tests / `-m`) vs mitmproxy loading by path (cost_xray/ on sys.path)."""
    try:
        from cost_xray.materialize_daemon import consume
    except ImportError:
        from materialize_daemon import consume
    consume(conn)


def _signal_materialize() -> None:
    """Wake the warm consumer for one sweep — a cheap `send` on the pipe (the proxy never tokenizes).
    Fail-open: if signalling fails, capture is unaffected and the next turn (or the TUI's freshness
    kick) re-materializes."""
    try:
        _ensure_consumer().send(1)
    except Exception:
        pass


_BLOCK_SEEN: dict = {}
_BLOCK_CTX: dict = {}


def _write_http_record(d, record) -> None:
    """Append one HTTP turn in deduped form via the raw codec (block store + delta record). A
    per-dir seen-hash set, seeded once from the session's store, lets a long live session append only
    each turn's new blocks — the hot path stays linear in body size and writes far less. A paired
    per-dir keyframe ctx delta-encodes the message ref list across turns; lost on restart, so the next
    turn just re-keyframes (always correct). Runs on the proxy's serialized response hook; the hash
    work folds into the redaction this body already took."""
    key = str(d)
    seen = _BLOCK_SEEN.get(key)
    if seen is None:
        seen = raw_codec.load_hashes(d)
        _BLOCK_SEEN[key] = seen
        _BLOCK_CTX[key] = {}
    raw_codec.append_record(d, record, seen=seen, ctx=_BLOCK_CTX[key])


def _writer_loop() -> None:
    while True:
        job = _WRITE_Q.get()
        try:
            kind = job[0]
            if kind == "append_raw":
                _, d, rec = job
                with (d / "raw.jsonl").open("a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            elif kind == "meta":
                _, d, meta = job
                _update_meta_info(d, meta)
            elif kind == "kick":
                _signal_materialize()
        except Exception:
            pass
        finally:
            _WRITE_Q.task_done()


def _drain_writes_for_tests() -> None:
    """Wait for the background writer. Tests only; production never calls this."""
    _WRITE_Q.join()


def _parse_sse(text: str) -> tuple[list, dict | None]:
    """Parse an SSE body into structured events; return (events, usage)."""
    events, usage = [], {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        events.append(obj)
        u = obj.get("usage") or (obj.get("message") or {}).get("usage")
        if isinstance(u, dict):
            usage.update(u)
    return events, (usage or None)


def request(flow) -> None:
    if not _matched(flow):
        return
    try:
        body = json.loads(flow.request.content)
    except Exception:
        return
    try:
        d = _session_dir(flow, body)
        ts = flow.request.timestamp_start
        _update_meta(d, flow, body, ts)
    except Exception:
        pass


def response(flow) -> None:
    if not _matched(flow):
        return
    if flow.response.status_code == 101:
        return
    raw = flow.response.get_text(strict=False) or ""
    ctype = flow.response.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        events, usage = _parse_sse(raw)
        response_field = {"streaming": True, "events": events, "n_events": len(events)}
    else:
        try:
            parsed = json.loads(raw)
            usage = parsed.get("usage")
        except Exception:
            parsed, usage = raw, None
        response_field = {"streaming": False, "body": parsed}

    try:
        body = json.loads(flow.request.content)
    except Exception:
        body = None
    req_body = _redact_body(body) if body is not None else None

    record = {
        "ts": flow.request.timestamp_start,
        "host": flow.request.host,
        "path": flow.request.path,
        "model": body.get("model") if isinstance(body, dict) else None,
        "status": flow.response.status_code,
        "request_headers": _redact_headers(flow.request.headers),
        "request": req_body,
        "response": response_field,
        "usage": usage,
    }
    try:
        d = _session_dir(flow, body)
        _write_http_record(d, record)
        _enqueue(("kick",))                              # one HTTP response = one turn → materialize
    except Exception:
        pass


def _flow_meta(flow) -> dict:
    md = getattr(flow, "metadata", None)
    if not isinstance(md, dict):
        md = {}
        try:
            flow.metadata = md
        except Exception:
            pass
    return md


def _ws_record(flow, message) -> tuple[dict, dict | None]:
    txt = message.content.decode("utf-8", "replace") if message.content else ""
    try:
        obj = json.loads(txt)
    except Exception:
        obj = None
    rec = {
        "ts": getattr(message, "timestamp", flow.request.timestamp_start),
        "transport": "websocket",
        "host": flow.request.host,
        "path": flow.request.path,
        "from_client": bool(message.from_client),
        "type": obj.get("type") if isinstance(obj, dict) else None,
        "frame": _redact_body(obj) if obj is not None else txt,
        "size": len(message.content or b""),
    }
    return rec, obj


def websocket_message(flow) -> None:
    """Append each Codex WebSocket frame as it arrives.

    The hook does only cheap decoding/redaction and enqueues disk work. Tokenization and
    materialization run in the background after `response.completed`, so streaming is not
    blocked by analysis.
    """
    if not _matched(flow):
        return
    ws = getattr(flow, "websocket", None)
    if not ws or not ws.messages:
        return
    try:
        md = _flow_meta(flow)
        message = ws.messages[-1]
        rec, obj = _ws_record(flow, message)
        d = pathlib.Path(md["cost_xray_session_dir"]) if md.get("cost_xray_session_dir") else None
        if d is None:
            d = _session_dir(flow, obj if isinstance(obj, dict) else None)
            md["cost_xray_session_dir"] = str(d)
        md["cost_xray_realtime_ws"] = True
        _enqueue(("append_raw", d, rec))
        if isinstance(obj, dict) and message.from_client and obj.get("type") == "response.create":
            _enqueue(("meta", d, _meta_info(flow, obj, rec["ts"])))
        if isinstance(obj, dict) and obj.get("type") == "response.completed":
            _enqueue(("kick",))                          # Codex turn boundary → materialize (not per frame)
    except Exception:
        pass


def websocket_end(flow) -> None:
    """Codex's model call is `GET /responses` -> 101 -> a WebSocket carrying OpenAI-Responses
    JSON-RPC frames (`response.create` = the API request; `response.*` = the streamed reply),
    several turns over one socket. Normal capture happens per-frame in `websocket_message`
    via the background writer. This close hook is only the fallback for flows that were not
    marked realtime. The read-time adapter splits frames back into turns; the `response.create`
    frames hold instructions/tools/input."""
    if not _matched(flow):
        return
    if _flow_meta(flow).get("cost_xray_realtime_ws"):
        return
    ws = getattr(flow, "websocket", None)
    if not ws or not ws.messages:
        return
    try:
        last_create = None
        d = None
        for m in ws.messages:
            rec, obj = _ws_record(flow, m)
            if d is None:
                d = _session_dir(flow, obj if isinstance(obj, dict) else None)
            with (d / "raw.jsonl").open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if isinstance(obj, dict) and m.from_client and obj.get("type") == "response.create":
                last_create = (obj, rec["ts"])
        if last_create is not None:
            obj, ts = last_create
            _update_meta(d, flow, obj, ts)
        if d is not None:
            _enqueue(("kick",))                          # fallback close → materialize the socket's turns
    except Exception:
        pass
