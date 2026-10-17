from cost_xray.adapters import anthropic


def test_streamed_output_ref_resolves_to_captured_text():
    record = {
        "status": 200,
        "usage": {"output_tokens": 3},
        "request": {},
        "response": {
            "streaming": True,
            "events": [
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "fixture"},
                },
            ],
        },
    }
    assert anthropic.locate([record], {"turn": 0, "out": 0})["text"] == "fixture"
