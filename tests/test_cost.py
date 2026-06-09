"""Pricing tests — LiteLLM as the source, hardcoded fallback (docs/local/testing.md).

Modelled on codeburn's `models.test.ts`: assert relationships and invariants rather than
brittle absolute numbers, never hit the network, and exercise the LiteLLM parse/clamp path
by **injecting a fake `litellm` module** (so the suite runs with or without litellm
installed). The load-bearing safety property: a bad upstream entry must never produce a
negative or wildly inflated cost.
"""
from __future__ import annotations

import sys
import types

import pytest

from cost_xray import cost
from cost_xray.cost import _parse_litellm_entry, _safe_rate, rates


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


def test_rates_fallback_family_ordering():
    assert rates("claude-haiku-4-5")["input"] < rates("claude-sonnet-4-6")["input"]
    assert rates("claude-sonnet-4-6")["input"] < rates("claude-opus-4-8")["input"]


def test_rates_strips_1m_suffix_and_is_positive():
    r = rates("claude-opus-4-8[1m]")
    assert r["input"] > 0 and r["output"] > 0
    assert rates("claude-opus-4-8[1m]") == rates("claude-opus-4-8")


def test_rates_never_negative_for_any_model():
    for m in ("claude-opus-4-8", "gpt-5-codex", "mystery-model", ""):
        r = rates(m)
        assert r["input"] >= 0 and r["output"] >= 0


def test_litellm_path_via_injected_fake_module(monkeypatch):
    fake = types.ModuleType("litellm")
    fake.model_cost = {
        "some-new-model": {"input_cost_per_token": 2e-6, "output_cost_per_token": 8e-6},
        "bad-model": {"input_cost_per_token": -5, "output_cost_per_token": 1e-6},
    }
    monkeypatch.setitem(sys.modules, "litellm", fake)
    cost._LITELLM_CACHE.clear()
    r = rates("some-new-model")
    assert r["input"] == pytest.approx(2.0) and r["output"] == pytest.approx(8.0)
    assert r["cache_read"] == pytest.approx(0.2)
    assert r["cache_write"] == pytest.approx(2.5)
    cost._LITELLM_CACHE.clear()
    bad = rates("bad-model")
    assert bad["input"] == cost.PRICING["opus"]["input"]
    assert bad["output"] == cost.PRICING["opus"]["output"]
    cost._LITELLM_CACHE.clear()


def test_litellm_lookup_is_exact_not_substring(monkeypatch):
    fake = types.ModuleType("litellm")
    fake.model_cost = {"gpt-4o": {"input_cost_per_token": 2.5e-6, "output_cost_per_token": 1e-5}}
    monkeypatch.setitem(sys.modules, "litellm", fake)
    cost._LITELLM_CACHE.clear()
    assert rates("gpt-4o-mini")["input"] != 2.5e-6 * 1_000_000
    cost._LITELLM_CACHE.clear()
