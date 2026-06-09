"""Verification board — **shared machinery** (docs/design/verification.md).

Agent-neutral core only: the residual math, the `ref → coord` mapping, the cross-turn
aggregation/render, and the canonical bucket table + auto-add tripwire. Per-agent behaviour
(coverage on a real wire shape, accuracy against an exact tokenizer) lives in
`test_verify_<agent>.py`, because *every test is per-agent* — these are the pieces that genuinely
have no agent.
"""
from __future__ import annotations

import pytest

from cost_xray import verify
from cost_xray.events import BUCKET_OF, bucket_of, unknown_types


def test_residuals_basic_and_zero_truth():
    r = verify.residuals({"a": 110, "b": 90, "c": 5}, {"a": 100, "b": 100, "d": 0})
    assert r["a"]["abs_rel"] == pytest.approx(0.10)
    assert r["a"]["signed_rel"] == pytest.approx(0.10)
    assert r["b"]["signed_rel"] == pytest.approx(-0.10)
    assert r["c"]["truth"] == 0.0 and r["c"]["abs_rel"] is None
    assert r["d"]["abs_rel"] is None


def test_ref_coord_unifies_both_agents():
    assert verify.ref_coord({"field": "system"}) == ("system",)
    assert verify.ref_coord({"field": "instructions"}) == ("system",)
    assert verify.ref_coord({"field": "tools", "i": 3}) == ("tool", 3)
    assert verify.ref_coord({"msg": 2, "block": 1}) == ("msg", 2, 1)
    assert verify.ref_coord({"msg": 2}) == ("msg", 2)
    assert verify.ref_coord({"out": 0}) == ("out", 0)
    assert verify.ref_coord({"out": 0, "block": 1}) == ("out", 0, 1)
    assert verify.ref_coord(None) is None and verify.ref_coord({}) is None


def test_bucket_mapping_table_is_pinned():
    assert BUCKET_OF["text"] == "text"
    assert BUCKET_OF["thinking"] == "thinking"
    assert BUCKET_OF["redacted_thinking"] == "thinking"
    assert BUCKET_OF["tool_use"] == "tool_use"
    assert BUCKET_OF["server_tool_use"] == "tool_use"
    assert BUCKET_OF["tool_result"] == "tool_result"
    assert BUCKET_OF["web_search_tool_result"] == "tool_result"
    assert BUCKET_OF["reasoning"] == "thinking"
    assert BUCKET_OF["function_call"] == "tool_use"
    assert BUCKET_OF["function_call_output"] == "tool_result"


def test_auto_add_unknown_type_falls_through_to_itself():
    assert bucket_of("some_future_block") == "some_future_block"
    assert bucket_of(None) is None
    seen = unknown_types([{"type": "text"}, {"type": "some_future_block"}, {"type": None}])
    assert seen == {"some_future_block"}


def _rep(tool_truth, cov_ok=True):
    ours = {"Bash": 100.0, "Grep": 100.0}
    res = verify.residuals(ours, tool_truth)
    return {"facets": {"input_tool": {"ours": ours, "truth": tool_truth, "residual": res}},
            "coverage": {"ok": cov_ok, "missing": [] if cov_ok else [("msg", 0, 1)],
                         "orphan": [], "token_exact_delta": 0, "unknown_types": []}}


def test_aggregate_percentiles_and_completeness_rollup():
    reps = [_rep({"Bash": 100, "Grep": 110}), _rep({"Bash": 80, "Grep": 100}, cov_ok=False)]
    agg = verify.aggregate(reps)
    s = agg["facets"]["input_tool"]
    assert s["n"] == 4 and s["abs_rel_max"] >= s["abs_rel_p50"] >= 0
    assert "Bash" in s["by_key"] and "Grep" in s["by_key"]
    assert agg["coverage"] == {"turns": 2, "ok": 1, "with_missing": 1, "with_orphan": 0,
                               "token_mismatch": 0, "unknown_types": []}


def test_render_markdown_shape():
    agg = verify.aggregate([_rep({"Bash": 100, "Grep": 110})])
    md = verify.render_markdown(agg, title="unit")
    assert "unit — per-event accuracy" in md
    assert "input · schema" in md
    assert "total reconstruction" in md and "completeness" in md
