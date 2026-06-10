from __future__ import annotations

import json
import re

from cost_xray import events as ev

RESPONSES_PATH = "/backend-api/codex/responses"

_NAME_MAX = 40
_CWD_RE = re.compile(r"(?:working directory:|<cwd>)\s*(/[^\s\"'<,\\]+)", re.I)


def project_name(records):
    for rec in records[:200]:
        m = _CWD_RE.search(json.dumps(rec, ensure_ascii=False))
        if m:
            return m.group(1).rstrip("/") or None
    return None


def session_name(records):
    for rec in records:
        fr = _frame(rec) if isinstance(rec, dict) else None
        if not (isinstance(fr, dict) and fr.get("type") == "response.create"):
            continue
        for item in fr.get("input") or []:
            if isinstance(item, dict) and item.get("role") == "user":
                for b in item.get("content") or []:
                    if isinstance(b, dict) and b.get("type") in ("input_text", "text"):
                        t = " ".join((b.get("text") or "").split())
                        if t:
                            return t[:_NAME_MAX] + ("…" if len(t) > _NAME_MAX else "")
        return None
    return None


THINKING_R = 1.0

INCREMENTAL = False


def iter_turns(frames):
    turns, cur, history = [], None, []
    for r in frames:
        if not isinstance(r, dict):
            continue
        frame = _frame(r)
        if not isinstance(frame, dict):
            continue
        ftype = frame.get("type") or r.get("type")
        if ftype == "response.create":
            if cur is not None:
                turns.append(cur)
            new_input = frame.get("input") or []
            prior = [] if _has_compaction(new_input) else history
            cur = {"model": frame.get("model"), "path": r.get("path") or RESPONSES_PATH,
                   "ts": r.get("ts"), "instructions": frame.get("instructions"),
                   "tools": frame.get("tools") or [], "new_input": new_input,
                   "input": list(prior) + list(new_input),
                   "output": [], "usage": None}
        elif cur is None:
            continue
        elif ftype == "response.output_item.done":
            item = frame.get("item")
            if isinstance(item, dict):
                cur["output"].append(item)
        elif ftype == "response.completed":
            resp = frame.get("response") or {}
            cur["usage"] = resp.get("usage")
            if not cur["output"] and isinstance(resp.get("output"), list):
                cur["output"] = resp["output"]
            turns.append(cur)
            if _has_compaction(cur["new_input"]):
                history = []
            history = history + list(cur["new_input"]) + _carryable_output(cur["output"])
            cur = None
    if cur is not None:
        turns.append(cur)
    return turns


def _carryable_output(output):
    return [item for item in (output or [])
            if not (isinstance(item, dict) and item.get("type") == "reasoning")]


def _has_compaction(items):
    return any(isinstance(item, dict) and item.get("type") == "compaction"
               for item in (items or []))


def _frame(record):
    fr = record.get("frame")
    if isinstance(fr, str):
        import json
        try:
            return json.loads(fr)
        except Exception:
            return None
    return fr


def to_events(turn, i=0):
    out = []
    if not isinstance(turn, dict):
        return out

    if turn.get("instructions"):
        out.append(ev.make_event(zone="input", section="static", bucket="system",
                                 ref={"turn": i, "field": "instructions"},
                                 content=turn["instructions"]))

    for j, t in enumerate(turn.get("tools") or []):
        if not isinstance(t, dict):
            continue
        name = t.get("name") or t.get("type") or "unknown"
        out.append(ev.make_event(zone="input", section="static", bucket="schema",
                                 ref={"turn": i, "field": "tools", "i": j}, content=t, tool=name))

    id2tool = _call_index(turn.get("input"), turn.get("output"))
    for m, item in enumerate(turn.get("input") or []):
        out.extend(_item_events(item, ref={"turn": i, "msg": m}, zone="input",
                                 section="messages", id2tool=id2tool))

    for k, item in enumerate(turn.get("output") or []):
        out.extend(_item_events(item, ref={"turn": i, "out": k}, zone="output",
                                section=None, role="assistant", id2tool=id2tool))
    return out


def response_blocks(turn):
    return (turn.get("output") if isinstance(turn, dict) else None) or []


def raw_units(turn):
    if not isinstance(turn, dict):
        return []
    units = []
    if turn.get("instructions"):
        units.append((("system",), turn["instructions"]))
    for j, t in enumerate(turn.get("tools") or []):
        if isinstance(t, dict):
            units.append((("tool", j), t))
    for m, item in enumerate(turn.get("input") or []):
        units.extend(_item_units(item, ("msg", m)))
    for k, item in enumerate(turn.get("output") or []):
        units.extend(_item_units(item, ("out", k)))
    return units


def _item_units(item, base):
    if not isinstance(item, dict):
        return [(base, item)]
    t = item.get("type")
    if t == "function_call":
        return [(base, item)]
    if t == "function_call_output":
        return [(base, item.get("output"))]
    if t == "reasoning":
        return [(base, _reasoning_text(item))]
    if t == "message":
        content = item.get("content")
        if isinstance(content, str):
            return [((*base, 0), content)]
        if isinstance(content, list):
            return [((*base, i), _content_text(b) if isinstance(b, dict) else b)
                    for i, b in enumerate(content)]
        return []
    return [(base, _content_text(item.get("content")))]


def _call_index(*item_lists):
    idx = {}
    for items in item_lists:
        for it in (items or []):
            if isinstance(it, dict) and it.get("type") == "function_call" and it.get("call_id"):
                idx[it["call_id"]] = it.get("name")
    return idx


def _item_events(item, *, ref, zone, section, role=None, id2tool=None):
    if not isinstance(item, dict):
        return []
    t = item.get("type")
    r = role if role is not None else item.get("role")
    if t == "function_call":
        return [ev.make_event(zone=zone, section=section, wire_type=t, ref=ref, content=item,
                              tool=item.get("name"), id=item.get("call_id"), role=r)]
    if t == "function_call_output":
        cid = item.get("call_id")
        return [ev.make_event(zone=zone, section=section, wire_type=t, ref=ref,
                              content=item.get("output"), tool=(id2tool or {}).get(cid),
                              id=cid, role=r)]
    if t == "reasoning":
        return [ev.make_event(zone=zone, section=section, wire_type=t, ref=ref,
                              content=_reasoning_text(item), role=r)]
    if t == "message":
        return _message_content_events(item.get("content"), ref=ref, zone=zone,
                                       section=section, role=r)
    return [ev.make_event(zone=zone, section=section, wire_type=t or "message", ref=ref,
                          content=_content_text(item.get("content")), role=r)]


def _message_content_events(content, *, ref, zone, section, role=None):
    if isinstance(content, str):
        return [ev.make_event(zone=zone, section=section, wire_type="input_text",
                              ref={**ref, "block": 0}, content=content, role=role)]
    if isinstance(content, list):
        out = []
        for i, block in enumerate(content):
            if isinstance(block, dict):
                out.append(ev.make_event(zone=zone, section=section,
                                         wire_type=block.get("type") or "message",
                                         ref={**ref, "block": i},
                                         content=_content_text(block), role=role))
        return out
    return []


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("text", "")
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return ""


def _reasoning_text(item):
    parts = item.get("summary") or item.get("content") or []
    if isinstance(parts, list):
        return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in parts)
    return str(parts)


def window(record):
    m = _model(record).lower()
    if "gpt-5" in m or "codex" in m:
        return 1_000_000
    return 128_000


def usage(record):
    u = (record.get("usage") if isinstance(record, dict) else None) or {}
    cached = _int(u.get("cached_tokens")
                  or (u.get("input_tokens_details") or {}).get("cached_tokens")
                  or (u.get("prompt_tokens_details") or {}).get("cached_tokens"))
    prompt = _int(u.get("input_tokens") or u.get("prompt_tokens"))
    output = _int(u.get("output_tokens") or u.get("completion_tokens"))
    reasoning = _int((u.get("output_tokens_details") or {}).get("reasoning_tokens")
                     or (u.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    return ev.canon_usage(max(0, prompt - cached), cached, 0, output, write_1h=False,
                          output_reasoning=reasoning)


def output_thinking(record):
    r = usage(record).get("output_reasoning") or 0
    return r if r > 0 else None


def locate(records, ref):
    turns = iter_turns(records)
    t = ref.get("turn") if isinstance(ref, dict) else None
    if not isinstance(t, int) or not 0 <= t < len(turns):
        return None
    turn = turns[t]
    item = None
    if "msg" in ref:
        items = turn.get("input") or []
        item = items[ref["msg"]] if 0 <= ref["msg"] < len(items) else None
    elif "out" in ref:
        items = turn.get("output") or []
        item = items[ref["out"]] if 0 <= ref["out"] < len(items) else None
    elif ref.get("field") == "instructions":
        return turn.get("instructions")
    elif ref.get("field") == "tools":
        tools, i = turn.get("tools") or [], ref.get("i")
        return tools[i] if isinstance(i, int) and 0 <= i < len(tools) else None
    if item and "block" in ref and isinstance(item.get("content"), list):
        blocks = item["content"]
        return blocks[ref["block"]] if 0 <= ref["block"] < len(blocks) else item
    return item


def _model(record):
    if isinstance(record, dict):
        return record.get("model") or (record.get("request") or {}).get("model") or ""
    return ""


def _int(v):
    return int(v) if isinstance(v, (int, float)) else 0
