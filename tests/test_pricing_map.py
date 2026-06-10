from __future__ import annotations

import json
import os
import time

import pytest

from cost_xray import pricing_map


@pytest.fixture(autouse=True)
def _reset():
    pricing_map.reset()
    yield
    pricing_map.reset()


def test_fresh_cache_skips_fetch(monkeypatch, tmp_path):
    f = tmp_path / "litellm_prices.json"
    f.write_text(json.dumps({"claude-opus-4-8": {"input_cost_per_token": 1e-6,
                                                 "output_cost_per_token": 2e-6}}))
    monkeypatch.setattr(pricing_map, "_cache_file", lambda: f)
    monkeypatch.setattr(
        pricing_map, "_fetch",
        lambda: (_ for _ in ()).throw(AssertionError("must not fetch when cache is fresh")),
    )
    assert "claude-opus-4-8" in pricing_map.load()


def test_stale_cache_triggers_fetch(monkeypatch, tmp_path):
    f = tmp_path / "litellm_prices.json"
    f.write_text(json.dumps({"claude-opus-4-8": {"input_cost_per_token": 9e-9,
                                                 "output_cost_per_token": 9e-9}}))
    old = time.time() - pricing_map._TTL - 10
    os.utime(f, (old, old))
    monkeypatch.setattr(pricing_map, "_cache_file", lambda: f)
    fetched = {"claude-opus-4-8": {"input_cost_per_token": 5e-6, "output_cost_per_token": 2.5e-5}}
    monkeypatch.setattr(pricing_map, "_fetch", lambda: fetched)
    assert pricing_map.load()["claude-opus-4-8"]["input_cost_per_token"] == 5e-6
    assert json.loads(f.read_text())["claude-opus-4-8"]["input_cost_per_token"] == 5e-6


def test_fetch_failure_falls_back_to_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(pricing_map, "_cache_file", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(
        pricing_map, "_fetch", lambda: (_ for _ in ()).throw(OSError("network down")),
    )
    assert "claude-opus-4-8" in pricing_map.load()
