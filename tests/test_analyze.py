from __future__ import annotations

from cost_xray.analyze import analyze, ntok, window_for


def test_window_for_1m_context():
    assert window_for("claude-opus-4-8[1m]") == 1_000_000
    assert window_for("gpt-5-codex") == 1_000_000
    assert window_for("some-codex-model") == 1_000_000


def test_window_for_standard_claude():
    assert window_for("claude-opus-4-8") == 200_000
    assert window_for("claude-sonnet-4-6") == 200_000


def test_window_for_unknown_defaults_to_200k():
    assert window_for("") == 200_000
    assert window_for("mystery-model") == 200_000
    assert window_for(None) == 200_000


def test_ntok_handles_none_and_types():
    assert ntok(None) == 0
    assert ntok("") == 0
    assert ntok("hello world") > 0
    assert ntok({"a": "b"}) > 0
    assert ntok(["x", "y"]) > 0


def _sample_body():
    return {
        "model": "claude-opus-4-8[1m]",
        "system": "You are a helpful coding assistant.",
        "tools": [
            {"name": "Bash", "description": "run a shell command"},
            {"name": "mcp__github__create_issue", "description": "make an issue"},
            {"name": "mcp__github__list_repos", "description": "list repos"},
            {"name": "mcp__slack__post", "description": "post a message"},
        ],
        "messages": [
            {"role": "user", "content": "hello there"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "name": "mcp__github__create_issue", "input": {}},
            ]},
            {"role": "user", "content": [{"type": "tool_result", "content": "done"}]},
        ],
    }


def test_analyze_report_shape():
    r = analyze(_sample_body())
    assert r["model"] == "claude-opus-4-8[1m]"
    assert r["window"] == 1_000_000
    assert {c["name"] for c in r["categories"]} == {
        "System prompt", "Built-in tools", "MCP tools", "Messages",
    }
    assert "mcp_servers" in r
    assert "savings" in r


def test_used_equals_sum_of_categories_and_free_is_remainder():
    r = analyze(_sample_body())
    assert r["used"] == sum(c["tokens"] for c in r["categories"])
    assert r["free"] == r["window"] - r["used"]


def test_builtin_vs_mcp_split():
    r = analyze(_sample_body())
    cats = {c["name"]: c["tokens"] for c in r["categories"]}
    assert cats["Built-in tools"] > 0
    assert cats["MCP tools"] > cats["Built-in tools"]


def test_mcp_servers_grouped_and_sorted_by_tokens():
    r = analyze(_sample_body())
    servers = {s["server"]: s for s in r["mcp_servers"]}
    assert set(servers) == {"github", "slack"}
    assert servers["github"]["n_tools"] == 2
    assert servers["slack"]["n_tools"] == 1
    assert r["mcp_servers"][0]["server"] == "github"


def test_unused_mcp_server_is_flagged_for_savings():
    r = analyze(_sample_body())
    servers = {s["server"]: s for s in r["mcp_servers"]}
    assert servers["github"]["used"] is True
    assert servers["slack"]["used"] is False

    unused = r["savings"]["unused_mcp_servers"]
    assert [s["server"] for s in unused] == ["slack"]
    assert r["savings"]["unused_mcp_tokens"] == servers["slack"]["tokens"]


def test_message_kinds_classified():
    r = analyze(_sample_body())
    kinds = r["message_kinds"]
    assert set(kinds) >= {"user_text", "thinking", "text", "tool_use", "tool_result"}
    assert all(v > 0 for v in kinds.values())


def test_openai_responses_format_instructions_and_input():
    body = {
        "model": "gpt-5-codex",
        "instructions": "You are Codex.",
        "tools": [{"function": {"name": "run_tests"}}],
        "input": [{"role": "user", "content": "go"}],
    }
    r = analyze(body)
    cats = {c["name"]: c["tokens"] for c in r["categories"]}
    assert cats["System prompt"] > 0
    assert cats["Built-in tools"] > 0
    assert cats["Messages"] > 0
    assert r["window"] == 1_000_000


def test_empty_body_does_not_crash():
    r = analyze({})
    assert r["used"] == 0
    assert r["window"] == 200_000
    assert r["mcp_servers"] == []
    assert r["savings"]["unused_mcp_servers"] == []


def test_no_mcp_servers_means_no_savings():
    body = {
        "model": "claude-opus-4-8",
        "system": "hi",
        "tools": [{"name": "Bash"}, {"name": "Read"}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = analyze(body)
    assert r["mcp_servers"] == []
    assert r["savings"]["unused_mcp_tokens"] == 0
