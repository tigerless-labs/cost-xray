from cost_xray import materialize


def test_derived_round_trip_preserves_calibration():
    event = {
        "zone": "input",
        "section": "messages",
        "bucket": "text",
        "cal_tokens": 1.5,
        "cached": 0.5,
        "rewrote": 0.5,
        "fresh": 0.5,
        "output": 0,
        "usd": 0.01,
    }
    saved = materialize._derived_event(event)
    _, rows = materialize._enrich_from_derived({"model": "fixture", "events": [saved]})
    for field in ("cal_tokens", "cached", "rewrote", "fresh", "output"):
        assert rows[0][field] == event[field]
