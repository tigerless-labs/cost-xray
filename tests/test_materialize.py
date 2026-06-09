"""Materializer exact-mode gating (`materialize._exact_pins`, design/verification.md).

Production now pins the per-tool number and the output thinking bucket exactly when count_tokens
auth is present — but it must be **fail-open**: no auth ⇒ zero network calls and no pins (today's
tiktoken+THINKING_R path). And Codex output thinking is pinned **free** from the wire
(`reasoning_tokens`), with no count_tokens at all. count_tokens is monkeypatched, so this is
offline.
"""
from __future__ import annotations

from cost_xray import count_tokens, materialize
from cost_xray.adapters import anthropic, openai

MODEL = "claude-opus-4-8"


def _claude_rec():
    return {
        "path": "/v1/messages",
        "request": {
            "model": MODEL,
            "system": "You are helpful.",
            "tools": [{"name": "Bash", "description": "run"},
                      {"name": "mcp__github__create_issue", "description": "issue"}],
            "messages": [{"role": "user", "content": "hi"}],
        },
        "response": {"streaming": False, "body": {"content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "done"}]}},
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }


def _boom(*a, **k):
    raise AssertionError("count_tokens called without auth")


def test_no_auth_is_a_noop_zero_pins(monkeypatch):
    monkeypatch.setattr(count_tokens, "auth_headers", lambda *a, **k: None)
    monkeypatch.setattr(count_tokens, "tools_exact", _boom)
    monkeypatch.setattr(count_tokens, "output_thinking_tokens", _boom)
    monkeypatch.setattr(count_tokens, "input_thinking_tokens", _boom)
    rec = _claude_rec()
    evs = anthropic.to_events(rec)
    ia, oa = materialize._exact_pins(rec, evs, MODEL, "claude", None)
    assert ia is None and oa is None
    assert all("exact" not in e for e in evs)


def test_claude_auth_pins_tools_output_and_input_thinking(monkeypatch):
    monkeypatch.setattr(count_tokens, "auth_headers", lambda *a, **k: {"x": "y"})
    monkeypatch.setattr(count_tokens, "tools_exact", lambda tools, model, **k: [99] * len(tools))
    monkeypatch.setattr(count_tokens, "output_thinking_tokens", lambda blocks, model, **k: 7)
    monkeypatch.setattr(count_tokens, "input_thinking_tokens", lambda msgs, model, **k: 4)
    rec = _claude_rec()
    evs = anthropic.to_events(rec)
    ia, oa = materialize._exact_pins(rec, evs, MODEL, "claude", None)
    assert oa == {"thinking": 7}
    assert ia == {"thinking": 4}
    tools = [e for e in evs if (e.get("ref") or {}).get("field") == "tools"]
    assert tools and all(e["exact"] == 99 for e in tools)
    assert all("exact" not in e for e in evs if (e.get("ref") or {}).get("field") != "tools")


def test_codex_output_thinking_is_free_from_reasoning_tokens(monkeypatch):
    monkeypatch.setattr(count_tokens, "auth_headers", lambda *a, **k: None)
    turn = {"model": "gpt-5-codex", "path": "/backend-api/codex/responses",
            "instructions": "You are Codex.", "tools": [], "input": [],
            "output": [{"type": "reasoning", "summary": [], "content": [],
                        "encrypted_content": "blob"}],
            "usage": {"input_tokens": 100, "output_tokens": 60,
                      "output_tokens_details": {"reasoning_tokens": 25}}}
    evs = openai.to_events(turn)
    ia, oa = materialize._exact_pins(turn, evs, "gpt-5-codex", "codex", turn["path"])
    assert oa == {"thinking": 25}
    assert ia is None
    assert all("exact" not in e for e in evs)
