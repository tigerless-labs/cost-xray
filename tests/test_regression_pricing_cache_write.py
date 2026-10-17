from cost_xray import pricing_map


def test_cache_write_failure_preserves_fetched_prices(monkeypatch, tmp_path):
    fresh = {"claude-opus-4-8": {"input_cost_per_token": 0.123}}
    path = tmp_path / "prices.json"
    monkeypatch.setattr(pricing_map, "_cache_file", lambda: path)
    monkeypatch.setattr(pricing_map, "_fetch", lambda: fresh)
    monkeypatch.setattr(
        type(path), "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("read only"))
    )
    assert pricing_map._resolve() == fresh
