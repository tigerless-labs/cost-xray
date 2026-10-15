import os

from cost_xray import pricing_map


def test_future_pricing_cache_refreshes(monkeypatch, tmp_path):
    old = {"claude-opus-4-8": {"input_cost_per_token": 1}}
    fresh = {"claude-opus-4-8": {"input_cost_per_token": 2}}
    path = tmp_path / "prices.json"
    import json

    path.write_text(json.dumps(old))
    os.utime(path, (2000, 2000))
    monkeypatch.setattr(pricing_map, "_cache_file", lambda: path)
    monkeypatch.setattr(pricing_map.time, "time", lambda: 1000)
    monkeypatch.setattr(pricing_map, "_fetch", lambda: fresh)
    monkeypatch.setattr(pricing_map, "_state", {"map": old, "at": 2000})
    assert pricing_map.load() == fresh
