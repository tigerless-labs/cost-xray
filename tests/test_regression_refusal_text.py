from cost_xray.adapters import openai


def test_refusal_is_counted_and_covered_as_output_text():
    turn = {
        "input": [],
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "Cannot fulfill the fixture request."}],
            }
        ],
    }
    result = openai.to_events(turn)
    assert len(result) == 1 and result[0]["bucket"] == "text" and result[0]["tokens"] > 0
    assert dict(openai.raw_units(turn))[("out", 0, 0)] == "Cannot fulfill the fixture request."
