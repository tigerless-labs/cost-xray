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
    if wire_type is None:
        return None
    return BUCKET_OF.get(wire_type, wire_type)


def chash(content):
    s = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def mcp_server(tool):
    if tool and tool.startswith("mcp__"):
        parts = tool.split("__")
        return parts[1] if len(parts) >= 2 else "unknown"
    return None


def side_of(zone):
    return zone


def category(event):
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
    return {"fresh": int(fresh), "cached": int(cached), "rewrote": int(rewrote),
            "output": int(output), "write_1h": bool(write_1h),
            "output_reasoning": int(output_reasoning)}


def unknown_types(events):
    return {e["type"] for e in events if e["type"] is not None and e["type"] not in BUCKET_OF}
