from cost_xray.adapters import anthropic


def test_initial_block_content_and_tool_json_survive_stream_reconstruction():
    blocks = anthropic._blocks_from_sse(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "initial "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "tail"},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "name": "fixture", "id": "call", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"value":1}'},
            },
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "redacted_thinking", "data": "fixture-opaque"},
            },
        ]
    )
    assert blocks[0]["text"] == "initial tail"
    assert blocks[1]["input"] == {"value": 1}
    assert blocks[2]["data"] == "fixture-opaque"
