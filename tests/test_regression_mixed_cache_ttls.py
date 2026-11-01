from __future__ import annotations

import json

import pytest

from cost_xray import cost, count_tokens, materialize, pricing_map
from cost_xray.adapters import anthropic
from cost_xray.classify import reconcile_turn

MODEL = "fixture-cache-model"


@pytest.fixture(autouse=True)
def _offline_prices(monkeypatch):
    monkeypatch.setattr(pricing_map, "load", lambda: {MODEL: {
        "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6,
        "cache_creation_input_token_cost": 1.25e-6,
        "cache_read_input_token_cost": 0.1e-6}})
    monkeypatch.setattr(count_tokens, "auth_headers", lambda: None)


def _raw_usage(five=148, hour=100, with_total=True):
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation": {"ephemeral_5m_input_tokens": five,
                                "ephemeral_1h_input_tokens": hour}}
    if with_total:
        usage["cache_creation_input_tokens"] = five + hour
    return usage


def _record(usage):
    return {"model": MODEL, "request": {"model": MODEL, "system": "cached prefix",
                                       "messages": []},
            "response": {"streaming": False, "body": {"content": []}},
            "usage": usage, "status": 200}


@pytest.mark.parametrize("with_total", [True, False], ids=["explicit-total", "nested-total"])
@pytest.mark.parametrize("five,hour", [(148, 100), (0, 248), (248, 0)])
def test_direct_and_calibrated_bills_price_each_cache_ttl(five, hour, with_total):
    raw = _raw_usage(five, hour, with_total)
    record = _record(raw)
    normalized = anthropic.usage(record)
    assert normalized.get("rewrote_1h") == hour
    assert normalized["rewrote"] == five + hour
    # Independent arithmetic from the vendor contract: 5m writes1.25x input,
    # 1h writes2x input. No provider request or tokenizer oracle is used.
    expected = (five * 1.25 + hour * 2) / 1_000_000
    direct = cost.turn_cost(raw, MODEL)
    reconciled = reconcile_turn(anthropic.to_events(record), normalized, MODEL)
    assert direct["usd"]["rewrote"] == pytest.approx(expected)
    assert reconciled["bill"] == pytest.approx(expected)
    assert sum(e["rewrote_usd"] for e in reconciled["events"]) == pytest.approx(expected)
    assert sum(e["rewrote"] for e in reconciled["events"]) == pytest.approx(five + hour)


def test_refold_keeps_numerical_ttl_mix_and_legacy_boolean_compatibility():
    events = [{"tokens": 248, "cached": 0, "rewrote": 248, "fresh": 0, "output": 0}]
    usage = anthropic.usage(_record(_raw_usage()))
    _, enriched = materialize._enrich_from_derived({"model": MODEL, "usage": usage,
                                                   "events": events})
    assert sum(e["rewrote_usd"] for e in enriched) == pytest.approx(385 / 1_000_000)
    for legacy, multiplier in [(True, 2), (False, 1.25)]:
        _, old = materialize._enrich_from_derived({"model": MODEL,
            "usage": {"rewrote": 248, "write_1h": legacy}, "events": events})
        assert sum(e["rewrote_usd"] for e in old) == pytest.approx(248 * multiplier / 1_000_000)


def test_old_materialization_rebuilds_ttl_mix_from_raw_capture(tmp_path):
    session = tmp_path / "claude" / "s1"
    session.mkdir(parents=True)
    raw = json.dumps(_record(_raw_usage())) + "\n"
    (session / "raw.jsonl").write_text(raw, encoding="utf-8")
    (session / "derived.jsonl").write_text(json.dumps({"model": MODEL, "usage": {},
        "events": []}) + "\n", encoding="utf-8")
    (session / "summary.json").write_text(json.dumps({"agent": "claude", "n_turns": 1,
        "logic_version": materialize.LOGIC_VERSION - 1, "raw_offset": len(raw.encode())}),
        encoding="utf-8")
    result = materialize.materialize_session(session)
    assert result["bill"] == pytest.approx(385 / 1_000_000)
    derived = json.loads((session / "derived.jsonl").read_text(encoding="utf-8"))
    assert derived["usage"].get("rewrote_1h") == 100
    assert result["n_turns"] == 1


def test_usage_without_ttl_breakdown_keeps_default_five_minute_rate():
    raw = {"cache_creation_input_tokens": 248}
    normalized = anthropic.usage(_record(raw))
    assert "rewrote_1h" not in normalized
    assert normalized["write_1h"] is False
    assert cost.turn_cost(raw, MODEL)["usd"]["rewrote"] == pytest.approx(310 / 1_000_000)
