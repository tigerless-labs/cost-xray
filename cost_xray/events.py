"""Canonical Event — the one shape every agent's wire is translated into.

design.md §4 (event model) + §9 (per-agent adapter, shared classification).
An adapter (`adapters/<agent>.py`) turns raw wire bytes into a list of these dicts;
everything downstream (`classify.py`) is agent-agnostic and never branches on agent —
that is the whole point of the canonical seam.

Field groups (read an event top to bottom = its position in the TUI tree):
  path     : zone / section / bucket   — group_by these = the context/cost panel
  leaf     : tool / skill / role        — harvested drill-down (nullable)
  measure  : tokens                     — size; calibrated to `usage` at fold
  machinery: ref / id / type / hash     — provenance, dedup, call↔result join
"""
from __future__ import annotations

import hashlib
import json

from cost_xray.analyze import ntok

BUCKET_OF = {
    "text": "text",
    "thinking": "thinking",
    "redacted_thinking": "thinking",
    "tool_use": "tool_use",
    "server_tool_use": "tool_use",
    "tool_result": "tool_result",
    "web_search_tool_result": "tool_result",
    "reasoning": "thinking",
    "message": "text",
    "input_text": "text",
    "output_text": "text",
    "function_call": "tool_use",
    "function_call_output": "tool_result",
    "custom_tool_call": "tool_use",
    "custom_tool_call_output": "tool_result",
    "web_search_call": "tool_use",
}

BUCKETS = ("system", "schema", "text", "thinking", "tool_use", "tool_result")
SECTIONS = ("static", "messages", None)
ZONES = ("input", "output")


def bucket_of(wire_type):
    """Canonical bucket for a wire block `type`. Unknown → the verbatim type (so it
    self-appears as its own row); `system`/`schema` have no wire type and are assigned
    positionally by the adapter instead."""
    if wire_type is None:
        return None
    return BUCKET_OF.get(wire_type, wire_type)


def chash(content):
    s = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def mcp_server(tool):
    """Derived: server from an `mcp__<server>__<tool>` name, else None (builtin)."""
    if tool and tool.startswith("mcp__"):
        parts = tool.split("__")
        return parts[1] if len(parts) >= 2 else "unknown"
    return None


def side_of(zone):
    """Derived cost axis: input vs output. `zone` already is that (kept explicit)."""
    return zone


def category(event):
    """A (group, label) for the on-screen breakdown — Static flattened into Claude Code's
    `/context` categories (System prompt / System tools / MCP tools / Skills), Messages &
    Output split by bucket. The familiar `/context` view, but harvested from our events so
    it also carries cost. (Memory is a future overlay; for now it sits in System prompt.)"""
    z, s, b = event.get("zone"), event.get("section"), event.get("bucket")
    tool, skill, role = event.get("tool"), event.get("skill"), event.get("role")
    if z == "output":
        if b in ("tool_use", "tool_result"):
            return ("Output", "MCP tool use+output" if mcp_server(tool) else "system tool use+output")
        return ("Output", b or "—")
    if s == "static":
        if b == "system":
            return ("Static", "System prompt")
        if b == "schema":
            if skill:
                return ("Static", "Skills")
            return ("Static", "MCP tools" if mcp_server(tool) else "System tools")
        return ("Static", b or "—")
    if s == "messages":
        if b == "text":
            if tool == "Skill" and skill:
                return ("Messages", "Skill loads")
            return ("Messages", "user text" if role == "user" else "assistant text")
        if b in ("tool_use", "tool_result"):
            return ("Messages", "MCP tool use+output" if mcp_server(tool) else "system tool use+output")
        return ("Messages", b or "—")
    return ("?", b or "—")


def _count_content(content):
    """The bytes worth tokenizing for a wire block — its real *content*, not the JSON
    wrapper or verification metadata. Claude Code clears thinking *text* from history but
    leaves a large base64 `signature`; tokenizing `json.dumps(block)` would charge that
    signature (plus JSON escaping and structure keys) as context — measured to inflate
    Messages ~1.8× (thinking blocks there were 98% signature, 0% text). So we count the
    content field, not the whole block."""
    if not isinstance(content, dict):
        return content
    if isinstance(content.get("text"), str):
        return content["text"]
    if "thinking" in content:
        return (content.get("thinking") or "") + (content.get("signature") or "")
    if "content" in content:
        return content["content"]
    if "input" in content:
        return f"{content.get('name', '')} {json.dumps(content['input'], ensure_ascii=False, separators=(',', ':'))}"
    if "arguments" in content:
        return f"{content.get('name', '')} {content.get('arguments') or ''}"
    return content


def make_event(*, zone, section, ref, bucket=None, wire_type=None, content=None,
               tokens=None, tool=None, skill=None, role=None, id=None):
    """Build one canonical event.

    `bucket` may be given explicitly (`system`/`schema`, which carry no wire `type`)
    or derived from `wire_type`. `tokens` defaults to a tiktoken count of `content`
    (an approximation for Claude — calibrated to the turn's `usage` at fold time,
    see classify.reconcile_turn / docs/local/testing.md)."""
    return {
        "zone": zone,
        "section": section,
        "bucket": bucket if bucket is not None else bucket_of(wire_type),
        "tool": tool,
        "skill": skill,
        "role": role if role in ("user", "assistant") else None,
        "tokens": int(tokens) if tokens is not None else ntok(_count_content(content)),
        "ref": ref,
        "id": id,
        "type": wire_type,
        "hash": chash(content) if content is not None else None,
    }


def canon_usage(fresh, cached, rewrote, output, write_1h=False, output_reasoning=0):
    """Canonical per-turn usage — the shape every `adapter.usage()` returns (design.md
    §9). Field *locations* are per-agent (Anthropic `cache_read_input_tokens` vs OpenAI
    `cached_tokens`); this canonical shape is what the shared cost layer consumes.
    `output_reasoning` is the exact reasoning-token slice of `output` when the wire exposes it
    (OpenAI/Codex `output_tokens_details.reasoning_tokens`); 0 otherwise — it lets the read layer
    pin the output thinking bucket exactly, for free (verification.md)."""
    return {"fresh": int(fresh), "cached": int(cached), "rewrote": int(rewrote),
            "output": int(output), "write_1h": bool(write_1h),
            "output_reasoning": int(output_reasoning)}


def unknown_types(events):
    """Wire `type` values not yet mapped to a bucket — the tripwire (design.md §4).

    These still render (bucket == the verbatim type); this set just tells you a new
    wire block type has appeared so you can give it a display name / placement."""
    return {e["type"] for e in events if e["type"] is not None and e["type"] not in BUCKET_OF}
