from cost_xray import addon


def test_multiline_sse_and_nonobject_neighbors():
    text = 'data: {"type":"message_delta",\ndata: "usage":{"output_tokens":7}}\n\ndata: null\n\ndata: [DONE]\n\n'
    events, usage = addon._parse_sse(text)
    assert events == [{"type": "message_delta", "usage": {"output_tokens": 7}}]
    assert usage == {"output_tokens": 7}


def test_unicode_separators_remain_inside_sse_json_strings():
    import json
    payload = {"type": "content_block_delta", "delta": {"text": "first\u2028second"}}
    text = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    events, _ = addon._parse_sse(text)
    assert events == [payload]
