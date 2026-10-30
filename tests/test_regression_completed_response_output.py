from cost_xray.adapters import openai


def test_completion_payload_replaces_partial_streamed_output():
    first = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "first"}],
    }
    second = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "second"}],
    }
    turns = openai.iter_turns(
        [
            {"frame": {"type": "response.create", "input": []}},
            {"frame": {"type": "response.output_item.done", "item": first}},
            {
                "frame": {
                    "type": "response.completed",
                    "response": {
                        "model": "resolved-fixture-model",
                        "usage": {"output_tokens": 2},
                        "output": [first, second],
                    },
                }
            },
        ]
    )
    assert turns[0]["output"] == [first, second]
    assert turns[0]["model"] == "resolved-fixture-model"
