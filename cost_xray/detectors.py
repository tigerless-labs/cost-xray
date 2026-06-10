from __future__ import annotations

import re

from cost_xray.analyze import ntok

_HEADER = re.compile(r"(following skills? are available|available[ -]skills?\s*:|user-invocable skills?\s*:)", re.I)
_LOAD = re.compile(r"\s*Base directory for this skill:\s*(\S+)")
_ITEM = re.compile(r"^\s*[-*]\s+([A-Za-z0-9][\w:-]{1,63})\s*[:\-—]\s+(.{8,})$")


def skill_ads(text):
    if not isinstance(text, str) or not _HEADER.search(text):
        return []
    out, started = [], False
    for line in text.splitlines():
        if not started:
            if _HEADER.search(line):
                started = True
            continue
        if out and line.lstrip().startswith("#"):
            break
        m = _ITEM.match(line)
        if m:
            name, desc = m.group(1).strip(), m.group(2).strip()
            out.append({"name": name, "description": desc, "span": line, "tokens": ntok(line)})
    return out


def skill_load(text):
    if not isinstance(text, str):
        return None
    m = _LOAD.match(text)
    if not m:
        return None
    return m.group(1).rstrip("/").split("/")[-1] or None
