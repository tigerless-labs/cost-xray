from __future__ import annotations

import pytest

from cost_xray import count_tokens, verify
from cost_xray.adapters import anthropic
from cost_xray.events import _count_content

MODEL = "claude-opus-4-8"


def _record():
    return {
        "request": {
            "model": MODEL,
            "system": "You are a careful coding assistant.",
            "tools": [{"name": "Bash", "description": "run a shell command"},
                      {"name": "mcp__github__create_issue", "description": "open an issue"}],
            "messages": [
                {"role": "user", "content": "list the files and open an issue"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "run ls first", "signature": "c2ln"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"cmd": "ls"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.py b.py"}]},
            ],
        },
        "response": {"streaming": False, "body": {"content": [
            {"type": "text", "text": "Found two files; opening the issue now."}]}},
        "usage": {"input_tokens": 50, "cache_read_input_tokens": 1000,
                  "cache_creation": {"ephemeral_1h_input_tokens": 200}, "output_tokens": 10},
        "status": 200,
    }


def test_coverage_complete_turn_is_ok():
    cov = verify.coverage(_record(), anthropic)
    assert cov["ok"] is True
    assert cov["missing"] == [] and cov["orphan"] == []
    assert cov["token_exact_delta"] == 0
    assert cov["expected"] == 8 and cov["covered"] == 8
    assert cov["unknown_types"] == []


def test_coverage_flags_a_structurally_dropped_block():
    rec = _record()
    rec["request"]["messages"][0]["content"] = [{"type": "text", "text": "hi"}, 12345]
    cov = verify.coverage(rec, anthropic)
    assert cov["ok"] is False
    assert ("msg", 0, 1) in cov["missing"]


def test_coverage_token_guard_catches_a_missing_event():
    rec = _record()
    events = anthropic.to_events(rec)
    kept = [e for e in events if e.get("type") != "tool_result"]
    cov = verify.coverage(rec, anthropic, kept)
    assert cov["token_exact_delta"] != 0 and cov["ok"] is False


def test_coverage_unknown_type_tripwire():
    rec = _record()
    rec["request"]["messages"][0]["content"] = [{"type": "brand_new_block", "text": "x"}]
    cov = verify.coverage(rec, anthropic)
    assert "brand_new_block" in cov["unknown_types"]


def _char_counter(payload, headers):
    n = 0
    for msg in payload["messages"]:
        c = msg.get("content")
        if isinstance(c, list):
            for b in c:
                p = _count_content(b)
                n += len(p) if isinstance(p, str) else 0
    return n


def test_per_output_event_tokens_diffs_text_and_thinking_in_place():
    blocks = [{"type": "thinking", "thinking": "AB", "signature": "CD"},
              {"type": "text", "text": "hello"},
              {"type": "tool_use", "name": "Bash", "input": {"x": 1}}]
    out = count_tokens.per_output_event_tokens(blocks, MODEL, _http=_char_counter)
    assert out == [("thinking", 4), ("text", 5)]


def test_per_output_event_tokens_none_when_no_diffable_blocks():
    assert count_tokens.per_output_event_tokens([], MODEL, _http=_char_counter) is None
    tool_only = [{"type": "tool_use", "name": "Bash", "input": {}}]
    assert count_tokens.per_output_event_tokens(tool_only, MODEL, _http=_char_counter) is None


def test_per_output_bucket_tokens_leave_one_out_all_three():
    blocks = [{"type": "thinking", "thinking": "AB", "signature": "CD"},
              {"type": "text", "text": "hello"},
              {"type": "tool_use", "name": "Bash", "input": {"x": 1}}]
    out = count_tokens.per_output_bucket_tokens(blocks, MODEL, _http=_char_counter)
    assert out == {"thinking": 4, "text": 5, "tool_io": 13}


def test_bench_turn_wires_facets_and_residuals():
    rec = _record()
    anchors = {"system": 30, "tools": 120, "static": 150, "messages": 900, "thinking": 700}
    per_tool = [("tool:Bash", 60), ("tool:mcp__github__create_issue", 60)]
    per_bucket = {"thinking": 700, "text": 40, "tool_io": 160, "structure": 0}
    per_output = [("text", 10)]

    rep = verify.bench_turn(rec, MODEL, anthropic, anchors=anchors, per_tool=per_tool,
                            per_bucket=per_bucket, per_output=per_output, thinking_r=0.39)

    assert set(rep["facets"]) == {"input_source", "input_tool", "input_bucket", "output_bucket"}
    for f in rep["facets"].values():
        assert f["residual"] == verify.residuals(f["ours"], f["truth"])
    assert rep["facets"]["input_tool"]["truth"] == {"Bash": 60, "mcp__github__create_issue": 60}
    assert set(rep["facets"]["input_tool"]["ours"]) == {"Bash", "mcp__github__create_issue"}
    assert rep["facets"]["output_bucket"]["truth"] == {"text": 10}
    assert rep["coverage"]["ok"] is True


def test_bench_turn_skips_unavailable_truths():
    rep = verify.bench_turn(_record(), MODEL, anthropic)
    assert rep["facets"] == {}
    assert rep["coverage"]["ok"] is True


def test_bench_turn_per_event_message_and_output_facets():
    per_message = [(("msg", 0, 0), "text", 30), (("msg", 1, 0), "thinking", 100),
                   (("tool_io", "toolu_1"), "tool_io", 50)]
    rep = verify.bench_turn(_record(), MODEL, anthropic, per_message=per_message,
                            per_output_event=[("text", 10)])
    assert "input_message" in rep["facets"] and "output_event" in rep["facets"]
    im = rep["facets"]["input_message"]
    assert ("tool_io", "toolu_1") in im["ours"] and ("tool_io", "toolu_1") in im["truth"]
    assert ("msg", 1, 0) in im["ours"]
    for fn in ("input_message", "output_event"):
        f = rep["facets"][fn]
        assert f["residual"] == verify.residuals(f["ours"], f["truth"])


def test_bench_turn_pin_tools_matches_production_exact():
    per_tool = [("tool:Bash", 60), ("tool:mcp__github__create_issue", 80)]
    rep = verify.bench_turn(_record(), MODEL, anthropic, per_tool=per_tool, pin_tools=True)
    for k, r in rep["facets"]["input_tool"]["residual"].items():
        if r["abs_rel"] is not None:
            assert r["abs_rel"] == pytest.approx(0.0, abs=1e-9), (k, r)
    base = verify.bench_turn(_record(), MODEL, anthropic, per_tool=per_tool, pin_tools=False)
    assert any(r["abs_rel"] and r["abs_rel"] > 1e-6
               for r in base["facets"]["input_tool"]["residual"].values())
