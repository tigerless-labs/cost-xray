from cost_xray.adapters import openai


def test_custom_tool_calls_preserve_content_and_identity():
    call = {
        "type": "custom_tool_call",
        "name": "fixture",
        "call_id": "call-fixture",
        "input": "print(42)",
    }
    result = {"type": "custom_tool_call_output", "call_id": "call-fixture", "output": "42"}
    turn = {"input": [call, result], "output": []}
    rows = openai.to_events(turn)
    assert [(r["tool"], r["id"]) for r in rows] == [("fixture", "call-fixture")] * 2
    assert rows[0]["tokens"] > 0
    assert openai.raw_units(turn) == [(("msg", 0), call), (("msg", 1), "42")]
