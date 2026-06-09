"""Live, end-to-end precision check against Anthropic's own `count_tokens` (design.md §6).

One test: take the latest captured Claude request, split its Messages into **our** bucket
classification by leave-one-out differencing (each bucket = A − count(messages without it),
no proportional scaling) plus a `structure` row for the per-message framing every single
removal leaves behind, add the exact static anchors (system + tools), and check the whole
thing reconstructs the real consumed input (`usage`). That chain — our decomposition →
real cost — is the precision demonstration.

Network + credentials required, so it is **opt-in** — skipped unless `COST_XRAY_LIVE=1`
AND real auth (OAuth login at `~/.claude/.credentials.json`, or an API key) AND a captured
Claude session are present:

    COST_XRAY_LIVE=1 pytest tests/test_live_count_tokens.py
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from cost_xray import count_tokens, verify
from cost_xray.adapters import anthropic

pytestmark = pytest.mark.skipif(
    os.environ.get("COST_XRAY_LIVE") != "1",
    reason="live count_tokens test — set COST_XRAY_LIVE=1 (needs OAuth/key + network)",
)


def _latest_claude_request():
    root = pathlib.Path("~/.cost-xray/sessions/claude").expanduser()
    best = None
    for raw in root.glob("*/raw.jsonl"):
        try:
            lines = raw.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r.get("request"), dict) and r.get("usage"):
                best = r
    return best


def _wire_input(usage):
    cc = usage.get("cache_creation")
    creation = (sum(v for v in cc.values() if isinstance(v, int)) if isinstance(cc, dict)
                else (usage.get("cache_creation_input_tokens") or 0))
    return (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0) + creation


def test_our_bucket_split_reconstructs_real_usage():
    """Our Messages buckets (leave-one-out, no scaling) + exact static anchors == real usage."""
    if count_tokens.auth_headers() is None:
        pytest.skip("no count_tokens auth (OAuth login or API key)")
    rec = _latest_claude_request()
    if rec is None:
        pytest.skip("no captured Claude session with usage")
    model = rec.get("model") or ""
    buckets = count_tokens.per_bucket_tokens(rec["request"], model)
    anchors = count_tokens.exact_anchors(rec["request"], model)
    if not buckets or anchors is None:
        pytest.skip("count_tokens unavailable (auth/network?)")

    assert all(v >= 0 for v in buckets.values())
    assert sum(buckets.values()) == anchors["messages"], (buckets, anchors["messages"])
    wire = _wire_input(rec["usage"])
    assert wire > 0
    assert abs(anchors["static"] + sum(buckets.values()) - wire) / wire < 0.005, \
        (anchors["static"], sum(buckets.values()), wire)


def test_output_per_bucket_pinned_thinking_vs_differencing():
    """The output axis, **mirroring the input method** (verification.md): the generated blocks are
    differenced per bucket (`per_output_bucket_tokens`, leave-one-out, thinking/text/tool_io), then
    our calibration **pins output `thinking` to that exact value** and tiktoken-proportions the rest
    — exactly as the input pins thinking via `count_tokens` instead of `THINKING_R`. With thinking
    pinned, `text` (and `tool_io`) should land close.

    Hard pins: the conservation floor (calibrated output total == wire output) and `thinking`
    residual ≈ 0 (it's pinned). `text`/`tool_io` get a real — but no longer thinking-contaminated —
    accuracy gate."""
    if count_tokens.auth_headers() is None:
        pytest.skip("no count_tokens auth (OAuth login or API key)")
    rec = _latest_claude_request()
    if rec is None:
        pytest.skip("no captured Claude session with usage")
    model = rec.get("model") or ""
    blocks = anthropic.response_blocks(rec)
    out_bkt = count_tokens.per_output_bucket_tokens(blocks, model)
    if not out_bkt:
        pytest.skip("captured turn has no countable output blocks")

    per_output = list(out_bkt.items())
    rep = verify.bench_turn(rec, model, anthropic, per_output=per_output,
                            output_thinking=out_bkt.get("thinking"),
                            thinking_r=anthropic.THINKING_R)
    res = rep["facets"]["output_bucket"]["residual"]
    assert rep["meta"]["output_tokens"] == anthropic.usage(rec)["output"]
    if "thinking" in res and res["thinking"]["abs_rel"] is not None:
        assert res["thinking"]["abs_rel"] < 1e-6
    for bkt in ("text", "tool_io"):
        r = res.get(bkt)
        if r and r["abs_rel"] is not None:
            assert r["abs_rel"] < 0.15, (bkt, r)
