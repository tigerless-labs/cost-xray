"""Detectors — the thin overlay layer beside the harvested tree (design.md §4).

These read CONTENT (not just position), so they live outside the positional adapter.
Each is **intermittent-safe**: it returns nothing when its pattern is absent, so a turn
that doesn't carry the thing is never corrupted.

`skill_ads` is the one *structured* detector: each available skill's `name` +
`description` (the SKILL.md frontmatter — a fixed structure) is injected into
`system`/reminder **intermittently** (not every turn) and with **no fixed slot**, so we
find it by structure wherever it lands. memory-file / system-reminder / never-called are
*heuristic* overlays computed at render time and live in the TUI layer.
"""
from __future__ import annotations

import re

from cost_xray.analyze import ntok

_HEADER = re.compile(r"(following skills? are available|available[ -]skills?\s*:|user-invocable skills?\s*:)", re.I)
_LOAD = re.compile(r"\s*Base directory for this skill:\s*(\S+)")
_ITEM = re.compile(r"^\s*[-*]\s+([A-Za-z0-9][\w:-]{1,63})\s*[:\-—]\s+(.{8,})$")


def skill_ads(text):
    """Per-skill ads in `text`: `[{name, description, span, tokens}]`, or `[]` if absent.

    `span` is the **original matched line** (verbatim, including its bullet) so the caller
    can carve it out of `system` and tokenise the remainder from real text — tokenizers are
    not strictly additive, so subtracting a reconstructed `"name: desc"` would leak a few
    tokens. Guarded by a header marker; reads bulleted `- name: description` items, skipping
    non-bulleted description continuations, until a markdown heading ends the block.
    Intermittent-safe: no header → `[]` (the common case)."""
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
    """The skill name of an injected SKILL.md body, or `None` if `text` isn't one.

    The harness prefixes a skill's loaded body with `Base directory for this skill:
    <path>/<skill>`; the last path segment is the skill. Anchored at the start so an
    assistant message merely quoting the phrase never mis-matches."""
    if not isinstance(text, str):
        return None
    m = _LOAD.match(text)
    if not m:
        return None
    return m.group(1).rstrip("/").split("/")[-1] or None
