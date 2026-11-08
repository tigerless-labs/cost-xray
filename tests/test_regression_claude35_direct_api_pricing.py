"""Regression: Claude 3.5 direct-API model names must use their real prices.

litellm's price map only carries the bedrock/vertex aliases for this family
(e.g. anthropic.claude-3-5-haiku-20241022-v1:0), so the direct-API names fell
through rates() to _DEFAULT ($5/$25 per M) and silently overstated costs
(6.25x for 3.5-haiku) with no warning.
"""
import json
import pathlib

import pytest

from cost_xray import pricing_map
from cost_xray.cost import rates, session_cost


def _bundled_map():
    p = pathlib.Path(pricing_map.__file__).with_name("data") / "litellm_prices.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _use_bundled_map(monkeypatch):
    bundled = _bundled_map()
    # the premise of this fix: the shipped map has no direct-API Claude 3.5 keys
    assert "claude-3-5-haiku-20241022" not in bundled
    assert "claude-3-5-sonnet-20241022" not in bundled
    monkeypatch.setattr(pricing_map, "load", lambda: bundled)


@pytest.mark.parametrize("model,inp,out", [
    ("claude-3-5-haiku-20241022", 0.8, 4.0),
    ("claude-3-5-haiku", 0.8, 4.0),
    ("claude-3-5-haiku-latest", 0.8, 4.0),
    ("claude-3-5-sonnet-20241022", 3.0, 15.0),
    ("claude-3-5-sonnet", 3.0, 15.0),
])
def test_claude_35_direct_api_names_priced(model, inp, out, monkeypatch):
    _use_bundled_map(monkeypatch)
    r = rates(model)
    assert r["input"] == pytest.approx(inp)
    assert r["output"] == pytest.approx(out)


def test_claude_35_haiku_session_not_overstated(monkeypatch):
    _use_bundled_map(monkeypatch)
    recs = [{"model": "claude-3-5-haiku-20241022", "status": 200,
             "usage": {"input_tokens": 10000, "output_tokens": 2000,
                       "cache_read_input_tokens": 50000,
                       "cache_creation_input_tokens": 30000}}]
    assert session_cost(recs)["total_usd"] == pytest.approx(0.05)  # was 0.3125


def test_price_map_still_wins_for_models_it_lists(monkeypatch):
    monkeypatch.setattr(pricing_map, "load", lambda: {
        "claude-3-5-haiku-20241022": {"input_cost_per_token": 1e-6,
                                      "output_cost_per_token": 4e-6}})
    r = rates("claude-3-5-haiku-20241022")
    assert r["input"] == pytest.approx(1.0) and r["output"] == pytest.approx(4.0)
