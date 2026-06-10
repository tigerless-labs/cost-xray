from __future__ import annotations

from cost_xray.adapters import anthropic, openai

_BY_AGENT = {
    "claude": anthropic,
    "codex": openai,
    "cursor": anthropic,
}


def adapter_for(agent=None, path=None):
    if agent and agent in _BY_AGENT:
        return _BY_AGENT[agent]
    p = path or ""
    if "/responses" in p or "/chat/completions" in p:
        return openai
    return anthropic


def _path(record, path):
    if path is None and isinstance(record, dict):
        return record.get("path")
    return path


def iter_turns(records, *, agent=None, path=None):
    return adapter_for(agent=agent, path=path).iter_turns(records)


def to_events(record, turn=0, *, agent=None, path=None):
    return adapter_for(agent=agent, path=_path(record, path)).to_events(record, turn)


def window(record, *, agent=None, path=None):
    return adapter_for(agent=agent, path=_path(record, path)).window(record)


def usage(record, *, agent=None, path=None):
    return adapter_for(agent=agent, path=_path(record, path)).usage(record)


def thinking_r(*, agent=None, path=None):
    return getattr(adapter_for(agent=agent, path=path), "THINKING_R", 1.0)


def incremental(*, agent=None, path=None):
    return getattr(adapter_for(agent=agent, path=path), "INCREMENTAL", False)


def session_name(records, *, agent=None, path=None):
    fn = getattr(adapter_for(agent=agent, path=path), "session_name", None)
    return fn(records) if fn else None


def project_name(records, *, agent=None, path=None):
    fn = getattr(adapter_for(agent=agent, path=path), "project_name", None)
    return fn(records) if fn else None


def locate(records, ref, *, agent=None, path=None):
    return adapter_for(agent=agent, path=path).locate(records, ref)


def response_blocks(record, *, agent=None, path=None):
    return adapter_for(agent=agent, path=_path(record, path)).response_blocks(record)


def output_thinking(record, *, agent=None, path=None):
    fn = getattr(adapter_for(agent=agent, path=_path(record, path)), "output_thinking", None)
    return fn(record) if fn else None


def raw_units(record, *, agent=None, path=None):
    return adapter_for(agent=agent, path=_path(record, path)).raw_units(record)
