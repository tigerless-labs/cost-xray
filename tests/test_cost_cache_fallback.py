"""Cache-fallback must be provider-aware (#19): Anthropic 0.1x/1.25x,
OpenAI 0.5x/0x — explicit litellm fields always win over the fallback."""
from cost_xray.cost import _parse_litellm_entry


def _close(a, b):
    return abs(a - b) < 1e-9


def test_openai_entry_without_cache_fields_gets_openai_multipliers():
    entry = {"input_cost_per_token": 0.0000025, "output_cost_per_token": 0.00001}
    r = _parse_litellm_entry(entry, "gpt-4o")
    assert _close(r["cache_read"], 1.25)   # 0.5x, not Anthropic's 0.1x
    assert r["cache_write"] == 0.0          # no invented surcharge


def test_anthropic_entry_without_cache_fields_keeps_anthropic_multipliers():
    entry = {"input_cost_per_token": 0.000005, "output_cost_per_token": 0.000025}
    r = _parse_litellm_entry(entry, "claude-opus-4-8")
    assert _close(r["cache_read"], 0.5)
    assert _close(r["cache_write"], 6.25)


def test_explicit_cache_fields_win_over_any_fallback():
    entry = {"input_cost_per_token": 0.0000025, "output_cost_per_token": 0.00001,
             "cache_read_input_token_cost": 0.00000125,
             "cache_creation_input_token_cost": 0.000003125}
    r = _parse_litellm_entry(entry, "gpt-4o")
    assert _close(r["cache_read"], 1.25)
    assert _close(r["cache_write"], 3.125)
