from __future__ import annotations

NAME_MAX = 40


def human_label(texts):
    for raw in texts:
        t = " ".join((raw or "").split())
        if t and not t.startswith("<"):
            return t[:NAME_MAX] + ("…" if len(t) > NAME_MAX else "")
    return None
