from __future__ import annotations

import json
import re

from cost_xray import detectors
from cost_xray import events as ev

_NAME_MAX = 40
_CWD_RE = re.compile(
    r"invoked in the following environment:.{0,200}?[Pp]rimary working directory:\s*(/[^\s\"',\\]+)",
    re.S)


def project_name(records):
    for rec in records[:50]:
        req = _request(rec)
        if not isinstance(req, dict):
            continue
        m = _CWD_RE.search(json.dumps(req, ensure_ascii=False))
        if m:
            return m.group(1).rstrip("/") or None
    return None


def _first_human_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = (b.get("text") or "").strip()
                if t and not t.startswith("<"):
                    return t
    return None


def session_name(records):
    best = None
    for rec in records:
        req = _request(rec)
        msgs = req.get("messages") if isinstance(req, dict) else None
        if msgs and (best is None or len(msgs) > len(best)):
            best = msgs
    for m in best or []:
        if m.get("role") != "user":
            continue
        t = _first_human_text(m.get("content"))
        if not t:
            continue
        t = " ".join(t.split())
        if t.lower() == "quota":
            continue
        return t[:_NAME_MAX] + ("…" if len(t) > _NAME_MAX else "")
    return None


THINKING_R = 0.39

INCREMENTAL = True


def to_events(record, turn=0):
    req = _request(record)
    out = []
    if not isinstance(req, dict):
        return out

    system = req.get("system")
    if system is None:
        system = req.get("instructions")
    if system:
        out.extend(_system_events(system, turn))

    for i, t in enumerate(req.get("tools") or []):
        if not isinstance(t, dict):
            continue
        name = t.get("name") or (t.get("function") or {}).get("name") or "unknown"
        out.append(ev.make_event(zone="input", section="static", bucket="schema",
                                 ref={"turn": turn, "field": "tools", "i": i},
                                 content=t, tool=name))

    messages = req.get("messages")
    if messages is None:
        messages = req.get("input") or []
    id2tool = _tool_use_index(messages)
    for m, msg in enumerate(messages):
        out.extend(_message_events(msg, turn, m, id2tool))

    content = _response_content(record)
    if isinstance(content, list):
        for j, b in enumerate(content):
            out.extend(_block_events(b, ref={"turn": turn, "out": j},
                                     zone="output", section=None, role="assistant"))
    return out


def _request(record):
    if isinstance(record, dict) and "request" in record:
        return record["request"]
    return record


def _response_content(record):
    r = record.get("response") if isinstance(record, dict) else None
    if not isinstance(r, dict):
        return None
    if r.get("streaming"):
        return _blocks_from_sse(r.get("events") or [])
    body = r.get("body")
    return body.get("content") if isinstance(body, dict) else None


def response_blocks(record):
    return _response_content(record) or []


def raw_units(record):
    req = _request(record) or {}
    if not isinstance(req, dict):
        return []
    units = []
    system = req.get("system")
    if system is None:
        system = req.get("instructions")
    if system is not None:
        units.append((("system",), system))
    for i, t in enumerate(req.get("tools") or []):
        units.append((("tool", i), t))
    messages = req.get("messages")
    if messages is None:
        messages = req.get("input") or []
    for m, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        c = msg.get("content")
        if isinstance(c, str):
            units.append((("msg", m, 0), c))
        elif isinstance(c, list):
            for b, block in enumerate(c):
                units.append((("msg", m, b), block))
    for j, block in enumerate(response_blocks(record)):
        units.append((("out", j), block))
    return units


def _ad_carve(full, ads, base_ref, *, zone, section, bucket, role=None):
    remainder = full
    for a in ads:
        remainder = remainder.replace(a["span"], "", 1)
    evs = [ev.make_event(zone=zone, section=section, bucket=bucket, ref=base_ref,
                         content=remainder, role=role)]
    for a in ads:
        evs.append(ev.make_event(
            zone="input", section="static", bucket="schema",
            ref={**base_ref, "skill": a["name"]},
            content=a["span"], tool="Skill", skill=a["name"]))
    return evs


def _system_events(system, turn):
    full = system if isinstance(system, str) else _join_text(system)
    ads = detectors.skill_ads(full)
    ref = {"turn": turn, "field": "system"}
    if not ads:
        return [ev.make_event(zone="input", section="static", bucket="system",
                              ref=ref, content=system)]
    return _ad_carve(full, ads, ref, zone="input", section="static", bucket="system")


def _text_slot(text, ref, role, content):
    ads = detectors.skill_ads(text)
    if ads:
        return _ad_carve(text, ads, ref, zone="input", section="messages", bucket="text", role=role)
    sk = detectors.skill_load(text)
    return [ev.make_event(zone="input", section="messages", bucket="text", wire_type="text",
                          ref=ref, content=content, role=role,
                          tool=("Skill" if sk else None), skill=sk)]


def _join_text(blocks):
    if isinstance(blocks, list):
        return "\n".join((b.get("text") or "") if isinstance(b, dict) else str(b) for b in blocks)
    return blocks if isinstance(blocks, str) else ""


def _tool_use_index(messages):
    idx = {}
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    idx[b["id"]] = b.get("name")
    return idx


def _message_events(msg, turn, m, id2tool):
    if not isinstance(msg, dict):
        return []
    role = msg.get("role")
    content = msg.get("content")
    if isinstance(content, str):
        return _text_slot(content, {"turn": turn, "msg": m, "block": 0},
                          role if role in ("user", "assistant") else "user", content)
    if isinstance(content, list):
        out = []
        for b, block in enumerate(content):
            out.extend(_block_events(block, ref={"turn": turn, "msg": m, "block": b},
                                     zone="input", section="messages", role=role,
                                     id2tool=id2tool))
        return out
    return []


def _block_events(block, *, ref, zone, section, role=None, id2tool=None):
    if not isinstance(block, dict):
        return []
    bt = block.get("type")
    if bt == "text" and zone == "input" and section == "messages":
        return _text_slot(block.get("text") or "", ref, role, block)
    tool = skill = call_id = None
    if bt == "tool_use":
        tool, call_id = block.get("name"), block.get("id")
    elif bt == "tool_result":
        call_id = block.get("tool_use_id")
        tool = (id2tool or {}).get(call_id)
    return [ev.make_event(zone=zone, section=section, wire_type=bt, ref=ref,
                          content=block, tool=tool, skill=skill, role=role, id=call_id)]


def _blocks_from_sse(sse_events):
    blocks, order = {}, []
    for e in sse_events:
        et = e.get("type")
        if et == "content_block_start":
            idx = e.get("index")
            cb = e.get("content_block") or {}
            blocks[idx] = {"type": cb.get("type"), "name": cb.get("name"),
                           "id": cb.get("id"), "_text": "", "_json": "",
                           "_sig": cb.get("signature") or ""}
            order.append(idx)
        elif et == "content_block_delta":
            blk = blocks.get(e.get("index"))
            if blk is None:
                continue
            d = e.get("delta") or {}
            if d.get("type") == "text_delta":
                blk["_text"] += d.get("text", "")
            elif d.get("type") == "thinking_delta":
                blk["_text"] += d.get("thinking", "")
            elif d.get("type") == "signature_delta":
                blk["_sig"] += d.get("signature", "")
            elif d.get("type") == "input_json_delta":
                blk["_json"] += d.get("partial_json", "")
    out = []
    for idx in order:
        blk = blocks[idx]
        rec = {"type": blk["type"]}
        if blk["name"]:
            rec["name"] = blk["name"]
        if blk["id"]:
            rec["id"] = blk["id"]
        if blk["type"] in ("thinking", "redacted_thinking"):
            if blk["_text"]:
                rec["thinking"] = blk["_text"]
            if blk["_sig"]:
                rec["signature"] = blk["_sig"]
        elif blk["_text"]:
            rec["text"] = blk["_text"]
        if blk["_json"]:
            rec["input"] = blk["_json"]
        out.append(rec)
    return out


def iter_turns(records):
    from cost_xray.cost import is_completion
    return [r for r in records if isinstance(r, dict) and is_completion(r)]


def window(record):
    headers = (record.get("request_headers") or {}) if isinstance(record, dict) else {}
    betas = headers.get("anthropic-beta", "")
    if "context-1m" in betas or "[1m]" in _model(record).lower():
        return 1_000_000
    return 200_000


def usage(record):
    u = (record.get("usage") if isinstance(record, dict) else None) or {}
    cc = u.get("cache_creation_input_tokens")
    cc_1h = 0
    if cc is None:
        obj = u.get("cache_creation")
        if isinstance(obj, dict):
            cc_1h = _int(obj.get("ephemeral_1h_input_tokens"))
            cc = cc_1h + _int(obj.get("ephemeral_5m_input_tokens"))
        else:
            cc = _int(obj)
    return ev.canon_usage(_int(u.get("input_tokens")), _int(u.get("cache_read_input_tokens")),
                          _int(cc), _int(u.get("output_tokens")), write_1h=cc_1h > 0)


def locate(records, ref):
    turns = iter_turns(records)
    t = ref.get("turn") if isinstance(ref, dict) else None
    if not isinstance(t, int) or not 0 <= t < len(turns):
        return None
    rec, req = turns[t], _request(turns[t]) or {}
    if "msg" in ref and "block" in ref:
        msgs = req.get("messages") or req.get("input") or []
        try:
            content = msgs[ref["msg"]].get("content")
        except Exception:
            return None
        if isinstance(content, list):
            try:
                return content[ref["block"]]
            except Exception:
                return None
        return content
    if "out" in ref:
        body = (rec.get("response") or {}).get("body")
        try:
            return (body.get("content") or [])[ref["out"]] if isinstance(body, dict) else None
        except Exception:
            return None
    if ref.get("field") == "system":
        return req.get("system") or req.get("instructions")
    if ref.get("field") == "tools":
        try:
            return (req.get("tools") or [])[ref.get("i")]
        except Exception:
            return None
    return None


def _model(record):
    if isinstance(record, dict):
        return record.get("model") or (_request(record) or {}).get("model") or ""
    return ""


def _int(v):
    return int(v) if isinstance(v, (int, float)) else 0
