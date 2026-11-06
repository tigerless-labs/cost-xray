import pytest

from cost_xray.adapters import anthropic, openai


@pytest.mark.parametrize(
    "path", ["/tmp/My Project", "/tmp/Project, OReilly", "/tmp/O'Reilly"]
)
def test_project_paths_with_spaces_and_punctuation_remain_complete(path):
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
