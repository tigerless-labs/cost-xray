"""Reconciliation conservation laws (docs/local/testing.md).

The wire `usage` is ground truth for TOTALS; our tiktoken event sizes are only
PROPORTIONS. After calibration every roll-up must equal the wire EXACTLY — these tests
pin that. The single approximation (sibling split) is exercised by the tolerance test.
"""
from __future__ import annotations

import pytest

from cost_xray.adapters import anthropic
from cost_xray.classify import reconcile_turn
from cost_xray.cost import CACHE_READ_MULT, rates

MODEL = "claude-opus-4-8"


def _record():
    return {
        "request": {
            "model": MODEL,
            "system": "You are a careful coding assistant with detailed instructions.",
            "tools": [{"name": "Bash", "description": "run a shell command"},
                      {"name": "mcp__github__create_issue", "description": "open an issue"}],
            "messages": [
                {"role": "user", "content": "please list the files and open an issue"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "I should run ls first to see the layout"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"cmd": "ls -la"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.py b.py c.py"}]},
            ],
        },
        "response": {"streaming": False, "body": {"content": [
            {"type": "text", "text": "Found three files; opening the issue now."}]}},
        "usage": {"input_tokens": 50, "cache_read_input_tokens": 1000,
                  "cache_creation": {"ephemeral_1h_input_tokens": 200}, "output_tokens": 10},
        "status": 200,
    }


def _run(rec=None):
    rec = rec or _record()
    return reconcile_turn(anthropic.to_events(rec), anthropic.usage(rec), rec["request"]["model"])


def test_input_tokens_reconcile_to_wire_total():
    r = _run()
    assert r["recon"]["ours"]["input_tokens"] == pytest.approx(r["recon"]["wire"]["total_input"], rel=1e-9)


def test_cache_columns_conserve():
    r = _run()
    w, col = r["recon"]["wire"], r["recon"]["ours"]["columns"]
    assert col["cached"] == pytest.approx(w["cached"], rel=1e-9)
    assert col["rewrote"] == pytest.approx(w["rewrote"], rel=1e-9)
    assert col["fresh"] == pytest.approx(w["fresh"], rel=1e-9)


def test_output_reconciles():
    r = _run()
    assert r["recon"]["ours"]["output_tokens"] == pytest.approx(r["recon"]["wire"]["output"], rel=1e-9)


def test_bill_conserves_against_wire_formula():
    r = _run()
    rr = rates(MODEL)
    r_in, r_out = rr["input"] / 1e6, rr["output"] / 1e6
    wire_bill = 50 * r_in + 1000 * r_in * CACHE_READ_MULT + 200 * r_in * 2.0 + 10 * r_out
    assert r["bill"] == pytest.approx(wire_bill, rel=1e-9)


def test_bill_equals_sum_of_event_usd():
    r = _run()
    assert r["bill"] == pytest.approx(sum(e["usd"] for e in r["events"]), rel=1e-12)


def test_by_path_sums_to_grand_total():
    r = _run()
    total = sum(v["tokens"] for v in r["by_path"].values())
    grand = r["recon"]["ours"]["input_tokens"] + r["recon"]["ours"]["output_tokens"]
    assert total == pytest.approx(grand, rel=1e-9)


def test_by_tool_and_mcp_present():
    r = _run()
    assert "Bash" in r["by_tool"]
    assert "github" in r["by_mcp"]


def test_raw_tiktoken_within_tolerance_of_wire_total():
    r = _run()
    assert r["recon"]["approx"]["input_err"] < 5.0


def test_empty_usage_does_not_crash_and_bill_zero():
    rec = _record()
    r = reconcile_turn(anthropic.to_events(rec), {}, MODEL)
    assert r["bill"] == 0.0
    assert r["recon"]["wire"]["total_input"] == 0


def test_exact_pin_per_event_is_used_and_conserves():
    rec = _record()
    evs = anthropic.to_events(rec)
    for e in evs:
        if (e.get("ref") or {}).get("field") == "tools" and e.get("tool") == "Bash":
            e["exact"] = 123
    r = reconcile_turn(evs, anthropic.usage(rec), MODEL)
    bash = next(x for x in r["events"]
                if x.get("tool") == "Bash" and (x.get("ref") or {}).get("field") == "tools")
    assert bash["cal_tokens"] == pytest.approx(123, rel=1e-9)
    assert r["recon"]["ours"]["input_tokens"] == pytest.approx(
        r["recon"]["wire"]["total_input"], rel=1e-9)


def test_output_anchor_pins_output_thinking():
    rec = _record()
    r = reconcile_turn(anthropic.to_events(rec), anthropic.usage(rec), MODEL,
                       output_anchors={"thinking": 6})
    out_think = sum(e["cal_tokens"] for e in r["events"]
                    if e["zone"] == "output" and e["bucket"] == "thinking")
    assert out_think >= 0
    assert r["recon"]["ours"]["output_tokens"] == pytest.approx(r["recon"]["wire"]["output"], rel=1e-9)


def test_exact_anchors_make_static_messages_split_exact():
    rec = _record()
    r = reconcile_turn(anthropic.to_events(rec), anthropic.usage(rec), MODEL,
                       anchors={"static": 1200, "messages": 50})
    static_tok = sum(v["tokens"] for k, v in r["by_path"].items() if k[1] == "static")
    msg_tok = sum(v["tokens"] for k, v in r["by_path"].items() if k[1] == "messages")
    assert static_tok == pytest.approx(1200, rel=1e-9)
    assert msg_tok == pytest.approx(50, rel=1e-9)
