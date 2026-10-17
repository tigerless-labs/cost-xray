from cost_xray import count_tokens as ct


def test_failed_counts_can_recover(monkeypatch, tmp_path):
    monkeypatch.setattr(ct, "_TOOL_STORE", tmp_path / "tokens.json")
    ct._CACHE.clear()
    ct._TOOL_CACHE.clear()
    available = [False]

    def http(payload, headers):
        if not available[0]:
            return None
        return 100 + 10 * len(payload.get("tools", []))

    request = {"messages": [{"role": "user", "content": "fixture"}]}
    tools = [{"name": "fixture", "input_schema": {"type": "object"}}]
    assert ct.exact_anchors(request, "fixture", _http=http) is None
    assert ct.tools_exact(tools, "fixture", _http=http) is None
    available[0] = True
    assert ct.exact_anchors(request, "fixture", _http=http)["total"] == 100
    assert ct.tools_exact(tools, "fixture", _http=http) == [10]
