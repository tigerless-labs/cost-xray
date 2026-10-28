from cost_xray.adapters import anthropic, openai


def test_project_paths_with_spaces_remain_complete():
    path = "/tmp/My Project"
    claude = {
        "request": {
            "system": "invoked in the following environment: Primary working directory: "
            + path
            + "\nPlatform: fixture"
        }
    }
    codex = {
        "frame": {
            "type": "response.create",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<cwd>" + path + "</cwd>"}],
                }
            ],
        }
    }
    assert anthropic.project_name([claude]) == path
    assert openai.project_name([codex]) == path
