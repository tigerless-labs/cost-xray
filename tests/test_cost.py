from __future__ import annotations

import pytest

from cost_xray import cost, pricing_map
from cost_xray.cost import _parse_litellm_entry, _safe_rate, rates, turn_cost


def _use_map(monkeypatch, m):
    monkeypatch.setattr(pricing_map, "load", lambda: m)


def test_safe_rate_rejects_bad_and_caps_huge():
    assert _safe_rate(-1e-6) is None
    assert _safe_rate(float("nan")) is None
    assert _safe_rate(float("inf")) is None
    assert _safe_rate(None) is None
    assert _safe_rate(True) is None
    assert _safe_rate(2.0) == 1.0
    assert _safe_rate(1.5e-5) == 1.5e-5


def test_parse_litellm_entry_is_pure_and_per_million():
    assert _parse_litellm_entry(
        {"input_cost_per_token": 1.5e-5, "output_cost_per_token": 7.5e-5}
    ) == pytest.approx({"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75})
    assert _parse_litellm_entry({"input_cost_per_token": -1, "output_cost_per_token": 1}) is None
    assert _parse_litellm_entry({}) is None
    assert _parse_litellm_entry(None) is None


def test_parse_litellm_entry_uses_explicit_per_model_cache_costs():
    r = _parse_litellm_entry({"input_cost_per_token": 1e-5, "output_cost_per_token": 4e-5,
                              "cache_read_input_token_cost": 5e-6,
                              "cache_creation_input_token_cost": 1.25e-5})
    assert r["cache_read"] == pytest.approx(5.0)
    assert r["cache_write"] == pytest.approx(12.5)


def test_rates_reads_the_price_map(monkeypatch):
    _use_map(monkeypatch, {
        "some-new-model": {"input_cost_per_token": 2e-6, "output_cost_per_token": 8e-6},
    })
    r = rates("some-new-model")
    assert r["input"] == pytest.approx(2.0) and r["output"] == pytest.approx(8.0)
    assert r["cache_read"] == pytest.approx(0.2)
    assert r["cache_write"] == pytest.approx(2.5)


def test_rates_strips_1m_suffix_and_is_positive(monkeypatch):
    _use_map(monkeypatch, {
        "claude-opus-4-8": {"input_cost_per_token": 5e-6, "output_cost_per_token": 2.5e-5},
    })
    r = rates("claude-opus-4-8[1m]")
    assert r["input"] > 0 and r["output"] > 0
    assert rates("claude-opus-4-8[1m]") == rates("claude-opus-4-8")


def test_lookup_is_exact_not_substring(monkeypatch):
    _use_map(monkeypatch, {
        "gpt-4o": {"input_cost_per_token": 2.5e-6, "output_cost_per_token": 1e-5},
    })
    assert rates("gpt-4o-mini")["input"] != pytest.approx(2.5)


def test_override_used_only_when_map_misses(monkeypatch):
    _use_map(monkeypatch, {})
    r = rates("claude-fable-5")
    assert r["output"] > r["input"] > 0
    assert r["cache_read"] < r["input"]
    assert r["input"] != rates("some-unknown-model")["input"]


def test_map_wins_over_override(monkeypatch):
    _use_map(monkeypatch, {
        "claude-fable-5": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6},
    })
    assert rates("claude-fable-5")["input"] == pytest.approx(1.0)


def test_unknown_model_uses_current_default(monkeypatch):
    _use_map(monkeypatch, {})
    for m in ("mystery-model", "gpt-99", ""):
        r = rates(m)
        assert r["output"] > r["input"] > 0
        assert r["cache_read"] < r["input"]


def test_turn_cost_prices_cache_from_the_map_not_a_multiplier(monkeypatch):
    _use_map(monkeypatch, {
        "m": {"input_cost_per_token": 5e-6, "output_cost_per_token": 2.5e-5,
              "cache_read_input_token_cost": 3e-7,
              "cache_creation_input_token_cost": 9e-6},
    })
    r = rates("m")
    usage = {"input_tokens": 1000, "cache_read_input_tokens": 4000,
             "cache_creation_input_tokens": 2000, "output_tokens": 500}
    c = turn_cost(usage, "m")
    assert c["usd"]["cached"] == pytest.approx(4000 * r["cache_read"] / 1_000_000)
    assert c["usd"]["rewrote"] == pytest.approx(2000 * r["cache_write"] / 1_000_000)
    assert c["usd"]["cached"] != pytest.approx(4000 * r["input"] / 1_000_000 * cost.CACHE_READ_MULT)


def test_bundled_snapshot_loads_and_prices_opus(monkeypatch, tmp_path):
    monkeypatch.setattr(pricing_map, "_cache_file", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(pricing_map, "_fetch", lambda: (_ for _ in ()).throw(OSError("offline")))
    pricing_map.reset()
    assert "claude-opus-4-8" in pricing_map.load()
    r = rates("claude-opus-4-8")
    assert r["input"] > 0 and r["output"] > r["input"]
    assert r["cache_read"] < r["input"]
    pricing_map.reset()
