"""Per-agent adapter registry (design.md §9).

Each agent has exactly ONE adapter: raw wire record → canonical `Event[]`. This is
the only place code forks by agent; everything downstream of the Event is shared and
never says `if agent`. Dispatch is by the captured agent / request path (addon.py):
`/v1/messages` → Anthropic, `/responses` & `/chat/completions` → OpenAI.

The hard rule (design.md §9): **an adapter ends at "emit a canonical Event" — no
`group_by` may appear in adapter code.**
"""
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
    """Group raw.jsonl records into per-turn objects — one record per turn for Anthropic,
    a WebSocket frame-stream reassembly for Codex. Each agent knows its own raw granularity."""
    return adapter_for(agent=agent, path=path).iter_turns(records)


def to_events(record, turn=0, *, agent=None, path=None):
    """Translate one turn object into canonical events using the right adapter."""
    return adapter_for(agent=agent, path=_path(record, path)).to_events(record, turn)


def window(record, *, agent=None, path=None):
    """This turn's context window — each adapter knows where its own signal lives."""
    return adapter_for(agent=agent, path=_path(record, path)).window(record)


def usage(record, *, agent=None, path=None):
    """This turn's canonical usage — each adapter reads its own field locations."""
    return adapter_for(agent=agent, path=_path(record, path)).usage(record)


def thinking_r(*, agent=None, path=None):
    """This agent's thinking-bucket tokenizer correction for `classify.reconcile_turn`
    (Claude ≈ 0.39 for the base64 signature, Codex = 1.0 / exact). Keeps the agent-specific
    tokenizer fact in the adapter, not the shared reconciler."""
    return getattr(adapter_for(agent=agent, path=path), "THINKING_R", 1.0)


def incremental(*, agent=None, path=None):
    """Whether the materializer may parse only NEW raw bytes (record-per-turn agents) vs needing
    the whole stream (Codex frame reassembly). Lives in the adapter, not the materializer."""
    return getattr(adapter_for(agent=agent, path=path), "INCREMENTAL", False)


def session_name(records, *, agent=None, path=None):
    """A human session label — the **first user message** (truncated). Per-agent to parse (Claude
    re-sent `messages`, skipping the `quota` probe + injected `<…>` wrappers; Codex's first
    `response.create` `input`). `None` if not found → caller falls back to `session_id[:8]`."""
    fn = getattr(adapter_for(agent=agent, path=path), "session_name", None)
    return fn(records) if fn else None


def project_name(records, *, agent=None, path=None):
    """The project a session ran in — the **full cwd path** from the agent's env block (Claude
    `Primary working directory:`, Codex cwd). Full path = the project identity (CodeBurn-style: the
    literal cwd, no git-root guessing); the TUI shows its basename. `None` if absent."""
    fn = getattr(adapter_for(agent=agent, path=path), "project_name", None)
    return fn(records) if fn else None


def locate(records, ref, *, agent=None, path=None):
    """Reverse of `to_events`: the raw block/item a `ref` points at, for the TUI's lazy content
    fetch. Each agent knows its own raw shape (Anthropic record vs Codex frame stream), so the
    `drill` layer dispatches here instead of branching on agent."""
    return adapter_for(agent=agent, path=path).locate(records, ref)


def response_blocks(record, *, agent=None, path=None):
    """This turn's output content blocks (Anthropic SSE/body, Codex output items) — for the
    read layer's exact-mode output thinking-pin. Both adapters define it."""
    return adapter_for(agent=agent, path=_path(record, path)).response_blocks(record)


def output_thinking(record, *, agent=None, path=None):
    """Exact output-thinking tokens straight from the wire `usage`, when the agent exposes it
    (Codex `reasoning_tokens`); None otherwise (Anthropic — the caller pins via count_tokens).
    Agent-specific, so it lives behind the registry like `thinking_r` / `usage`."""
    fn = getattr(adapter_for(agent=agent, path=_path(record, path)), "output_thinking", None)
    return fn(record) if fn else None


def raw_units(record, *, agent=None, path=None):
    """`[(coord, content)]` for every countable raw unit (verify.coverage + exact-mode per-tool
    pinning). Each adapter walks its own wire shape; `coord` matches the `ref` `to_events` assigns."""
    return adapter_for(agent=agent, path=_path(record, path)).raw_units(record)
