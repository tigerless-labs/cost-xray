"""Regression: usage counts that arrive as JSON floats must price like ints.

Relays/proxies (LiteLLM-style gateways, Go/Rust re-serializers) emit every JSON
number as a float, and cost._u() only accepted ints — those sessions reported
$0.00 silently. The adapters already cast (_int) the same way.
"""
import pytest

from cost_xray.cost import session_cost, turn_cost


def _usage(as_float):
    u = {"input_tokens": 1000, "output_tokens": 500,
         "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 3000}
    return {k: float(v) if as_float else v for k, v in u.items()}


def test_float_usage_counts_price_the_same_as_ints():
    int_total = turn_cost(_usage(False), "claude-sonnet-4-5")["total_usd"]
    float_total = turn_cost(_usage(True), "claude-sonnet-4-5")["total_usd"]
    assert int_total > 0
    assert float_total == int_total


def test_float_usage_counts_keep_token_breakdown():
    t = turn_cost(_usage(True), "claude-sonnet-4-5")
    assert t["tokens"] == {"fresh": 1000, "cached": 2000, "rewrote": 3000, "output": 500}


def test_session_cost_accepts_float_usage_counts():
    recs = [{"model": "claude-sonnet-4-5", "status": 200, "usage": _usage(True)}]
    s = session_cost(recs)
    assert s["total_usd"] > 0
    assert s["tokens"]["fresh"] == 1000


@pytest.mark.parametrize(
    "invalid",
    [True, -1, -1.0, 1.5, float("nan"), float("inf"), float("-inf")],
)
def test_invalid_numeric_usage_counts_are_not_priced(invalid):
    result = turn_cost({"input_tokens": invalid}, "claude-sonnet-4-5")

    assert result["tokens"]["fresh"] == 0
    assert result["usd"]["fresh"] == 0


def test_invalid_direct_cache_count_falls_back_to_the_valid_breakdown():
    usage = {
        "cache_creation_input_tokens": float("nan"),
        "cache_creation": {
            "ephemeral_1h_input_tokens": 3.0,
            "ephemeral_5m_input_tokens": 7.0,
        },
    }

    assert turn_cost(usage, "claude-sonnet-4-5")["tokens"]["rewrote"] == 10
