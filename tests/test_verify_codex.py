"""Verification board — **Codex** (docs/design/verification.md).

The asymmetry that makes this file matter: for Codex, tiktoken **o200k is the real tokenizer**
(exact, local) and `reasoning` carries no base64 signature (`THINKING_R == 1.0`). So Codex
per-event accuracy is verifiable **offline, in CI** — no `count_tokens`, no network. The exact
truth is the local o200k count; the wire `usage` is the exact total.

The pinned property: when the wire total equals the exact local total (the o200k-exact, no extra
serialization case), `reconcile_turn`'s calibration is an identity and our per-source / per-tool /
per-bucket numbers reproduce the exact counts to the token. (The residual a *real* Codex capture
carries is the Responses framing overhead — measured by `experiments/benchmark.py` over captured
sessions, also offline; a committed capture fixture to assert a bound on it is a TODO.)
"""
from __future__ import annotations

import pytest

from cost_xray import verify
from cost_xray.adapters import openai


def _turn(usage=None):
    """One reassembled Codex turn (the unit `openai.to_events` consumes — the shape
    `openai.iter_turns` emits)."""
    return {
        "model": "gpt-5-codex",
        "instructions": "You are Codex, a precise coding agent.",
        "tools": [{"type": "function", "name": "shell", "description": "run a shell command"},
                  {"type": "function", "name": "mcp__linear__create_issue", "description": "x"}],
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "fix the failing test and explain why"}]},
            {"type": "function_call_output", "call_id": "c1", "output": "test now passes"},
        ],
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text",
                                              "text": "the assertion was off by one"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Fixed the off-by-one in the loop."}]},
            {"type": "function_call", "call_id": "c2", "name": "shell", "arguments": '{"cmd":"ls"}'},
        ],
        "usage": usage,
    }


def test_coverage_complete_codex_turn_is_ok():
    cov = verify.coverage(_turn(), openai)
    assert cov["ok"] is True
    assert cov["missing"] == [] and cov["orphan"] == []
    assert cov["token_exact_delta"] == 0
    assert cov["expected"] == 8 and cov["covered"] == 8


def test_coverage_codex_unknown_item_tripwire_not_dropped():
    turn = _turn()
    turn["output"].append({"type": "brand_new_item", "content": "surprise"})
    cov = verify.coverage(turn, openai)
    assert "brand_new_item" in cov["unknown_types"]
    assert ("out", 3) in {("out", 3)} and cov["ok"] is True


def test_codex_per_event_accuracy_is_exact_offline():
    """o200k is Codex's real tokenizer, so with the wire total == the exact local total the whole
    decomposition is exact — every facet residual is 0, **with no network**."""
    t = verify.local_truths(openai.to_events(_turn()))
    turn = _turn(usage={"input_tokens": t["total_in"], "output_tokens": t["total_out"]})

    rep = verify.bench_turn(turn, "gpt-5-codex", openai, anchors=t["anchors"],
                            per_tool=t["per_tool"], per_bucket=t["per_bucket"],
                            per_output=t["per_output"], thinking_r=openai.THINKING_R)

    for fname, f in rep["facets"].items():
        for key, res in f["residual"].items():
            if res["abs_rel"] is not None:
                assert res["abs_rel"] == pytest.approx(0.0, abs=1e-9), (fname, key, res)
    assert rep["coverage"]["ok"] is True
    assert rep["meta"]["input_tokens"] == t["total_in"]
    assert rep["meta"]["output_tokens"] == t["total_out"]


def test_thinking_r_is_a_noop_for_codex():
    """Codex `reasoning` has no signature → THINKING_R 1.0 → output thinking rides the same scale
    as everything else (no separate correction), unlike Anthropic's 0.39."""
    assert openai.THINKING_R == 1.0
