from cost_xray import drill


def test_structured_output_is_renderable_text():
    assert (
        drill._block_text(
            {"type": "function_call_output", "output": [{"type": "output_text", "text": "fixture"}]}
        )
        == "fixture"
    )
    value = drill._block_text({"output": {"result": 42}})
    assert isinstance(value, str)
    assert "42" in value
