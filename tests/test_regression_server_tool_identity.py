from cost_xray.adapters import anthropic


def test_server_tool_result_is_linked_to_its_producing_call():
    record = {
        "request": {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "fixture-id",
                            "name": "web_search",
                            "input": {"query": "fixture"},
                        },
                        {
                            "type": "web_search_tool_result",
                            "tool_use_id": "fixture-id",
                            "content": [],
                        },
                    ],
                }
            ]
        }
    }
    result = anthropic.to_events(record)
    assert len(result) == 2
    assert all(e["tool"] == "web_search" and e["id"] == "fixture-id" for e in result)
