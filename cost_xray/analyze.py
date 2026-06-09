"""Decompose a captured LLM request into context sources.

Input: the parsed JSON body of an Anthropic /v1/messages or OpenAI-style request
(captured by the mitmproxy addon). Output: a report with per-source token counts,
per-MCP-server breakdown, and which tools were actually used — so we can flag
"configured but never used" MCP servers (the dead-weight cost no log-based tool
sees, because tool schemas are not in the logs).

Tokenizer: tiktoken o200k_base. Exact for OpenAI/Codex; an approximation for
Claude (whose tokenizer is private) — totals are calibrated against the response
`usage` when available.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict

_NTOK_CACHE: dict[bytes, int] = {}
_NTOK_CACHE_MAX = 500_000

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("o200k_base")
    def ntok(x) -> int:
        if x is None:
            return 0
        s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, separators=(",", ":"))
        b = s.encode("utf-8")
        k = hashlib.sha1(b).digest()
        hit = _NTOK_CACHE.get(k)
        if hit is not None:
            return hit
        n = len(_ENC.encode(s))
        if len(_NTOK_CACHE) < _NTOK_CACHE_MAX:
            _NTOK_CACHE[k] = n
        return n
    TOKENIZER = "o200k_base"
except Exception:  # pragma: no cover
    def ntok(x) -> int:
        if x is None:
            return 0
        s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
        return -(-len(s) // 4)
    TOKENIZER = "chars/4"

def window_for(model: str, betas: str = "") -> int:
    ov = os.environ.get("COST_XRAY_WINDOW")
    if ov:
        try:
            return int(ov)
        except ValueError:
            pass
    m = (model or "").lower()
    b = (betas or "").lower()
    if "[1m]" in m or "1m" in b or "context-1m" in b or "gpt-5" in m or "codex" in m:
        return 1_000_000
    if "opus-4" in m or "sonnet-4" in m or "claude" in m:
        return 200_000
    return 200_000


def _mcp_server(name: str) -> str | None:
    if name.startswith("mcp__"):
        parts = name.split("__")
        return parts[1] if len(parts) >= 2 else "unknown"
    return None


def _tool_name(t: dict) -> str:
    return t.get("name") or (t.get("function") or {}).get("name") or "unknown"


def analyze(body: dict) -> dict:
    """Return a decomposition report for one captured request body."""
    model = body.get("model", "")
    system = body.get("system")
    if system is None:
        system = body.get("instructions")
    tools = body.get("tools") or []
    messages = body.get("messages")
    if messages is None:
        messages = body.get("input") or []

    sys_tok = ntok(system)

    builtin, mcp = [], []
    for t in tools:
        name = _tool_name(t)
        srv = _mcp_server(name)
        rec = {"name": name, "tokens": ntok(t), "server": srv}
        (mcp if srv else builtin).append(rec)

    builtin_tok = sum(t["tokens"] for t in builtin)
    mcp_tok = sum(t["tokens"] for t in mcp)

    servers: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "tools": [], "used": False})
    for t in mcp:
        s = servers[t["server"]]
        s["tokens"] += t["tokens"]
        s["tools"].append({"name": t["name"], "tokens": t["tokens"]})

    called: set[str] = set()
    msg_tok = 0
    msg_kinds = defaultdict(int)
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        role = msg.get("role") if isinstance(msg, dict) else None
        msg_tok += ntok(content if content is not None else msg)
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use":
                    called.add(b.get("name", ""))
                    msg_kinds["tool_use"] += ntok(b)
                elif bt == "tool_result":
                    msg_kinds["tool_result"] += ntok(b)
                elif bt == "thinking":
                    msg_kinds["thinking"] += ntok(b)
                elif bt == "text":
                    msg_kinds["text"] += ntok(b)
        elif isinstance(content, str):
            msg_kinds["text" if role != "user" else "user_text"] += ntok(content)

    for name, s in servers.items():
        s["used"] = any(_mcp_server(c) == name for c in called)

    used = sys_tok + builtin_tok + mcp_tok + msg_tok
    win = window_for(model)
    server_list = sorted(
        ({"server": k, **v, "n_tools": len(v["tools"])} for k, v in servers.items()),
        key=lambda x: -x["tokens"],
    )
    unused = [s for s in server_list if not s["used"]]
    unused_tok = sum(s["tokens"] for s in unused)

    return {
        "model": model,
        "tokenizer": TOKENIZER,
        "window": win,
        "used": used,
        "free": max(0, win - used),
        "categories": [
            {"name": "System prompt", "tokens": sys_tok},
            {"name": "Built-in tools", "tokens": builtin_tok},
            {"name": "MCP tools", "tokens": mcp_tok},
            {"name": "Messages", "tokens": msg_tok},
        ],
        "mcp_servers": server_list,
        "message_kinds": dict(msg_kinds),
        "savings": {
            "unused_mcp_servers": [
                {"server": s["server"], "tokens": s["tokens"], "n_tools": s["n_tools"]}
                for s in unused
            ],
            "unused_mcp_tokens": unused_tok,
        },
    }
