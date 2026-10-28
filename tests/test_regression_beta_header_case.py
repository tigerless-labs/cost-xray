from cost_xray.adapters import anthropic


def test_context_header_casing_does_not_change_window():
    record = {
        "model": "claude-fixture",
        "request_headers": {"Anthropic-Beta": "context-1m-fixture"},
    }
    assert anthropic.window(record) == anthropic.window(
        {**record, "request_headers": {"anthropic-beta": "context-1m-fixture"}}
    )
