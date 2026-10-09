from cost_xray import count_tokens as ct


def test_memory_tool_counts_expire(monkeypatch, tmp_path):
    monkeypatch.setattr(ct, "_TOOL_STORE", tmp_path / "tokens.json")
    ct._TOOL_CACHE.clear()
    value = [10]

    def http(payload, headers):
        return 100 + value[0] * len(payload.get("tools", []))

    tools = [{"name": "fixture", "input_schema": {"type": "object"}}]
    assert ct.tools_exact(tools, "fixture", _http=http, now=1000) == [10]
    value[0] = 20
    assert ct.tools_exact(tools, "fixture", _http=http, now=1000 + ct._TOOL_TTL + 1) == [20]
