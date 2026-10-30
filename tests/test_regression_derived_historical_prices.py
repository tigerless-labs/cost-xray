from cost_xray import materialize


def test_historical_components_survive_price_change(monkeypatch):
    event = {
        "zone": "input",
        "section": "messages",
        "bucket": "text",
        "cal_tokens": 3,
        "cached": 1,
        "rewrote": 1,
        "fresh": 1,
        "output": 0,
        "usd": 0.6,
        "cached_usd": 0.1,
        "rewrote_usd": 0.2,
        "fresh_usd": 0.3,
        "output_usd": 0,
    }
    saved = materialize._derived_event(event)
    monkeypatch.setattr(
        materialize,
        "rates",
        lambda model: dict(input=100, output=200, cache_read=50, cache_write=125),
    )
    _, rows = materialize._enrich_from_derived({"model": "fixture", "events": [saved]})
    for key in ("cached_usd", "rewrote_usd", "fresh_usd", "output_usd"):
        assert rows[0][key] == event[key]
