from cost_xray import cost, pricing_map


def test_provider_prefix_and_context_suffix_resolve_together(monkeypatch):
    entry = {"input_cost_per_token": 0.000002, "output_cost_per_token": 0.000006}
    monkeypatch.setattr(pricing_map, "load", lambda: {"fixture-model": entry})
    assert cost.rates("provider/fixture-model[1m]") == cost.rates("fixture-model")
