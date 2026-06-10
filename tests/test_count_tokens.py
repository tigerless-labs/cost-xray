from __future__ import annotations

from cost_xray import count_tokens


def _fake_counts(payload, headers):
    if payload.get("tools"):
        return 900
    if "system" in payload:
        return 350
    return 100


def test_exact_anchors_differences_cumulative_prefixes(monkeypatch):
    monkeypatch.setenv("COST_XRAY_ANTHROPIC_API_KEY", "sk-test")
    count_tokens._CACHE.clear()
    req = {"system": "S", "tools": [{"name": "t"}], "messages": [{"role": "user", "content": "hi"}]}
    a = count_tokens.exact_anchors(req, "claude-opus-4-8", _http=_fake_counts)
    assert a == {"system": 250, "tools": 550, "messages": 100, "thinking": 0,
                 "static": 800, "total": 900}


def test_exact_anchors_none_without_any_auth(monkeypatch):
    monkeypatch.delenv("COST_XRAY_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(count_tokens, "_oauth_token", lambda: None)
    count_tokens._CACHE.clear()
    assert count_tokens.exact_anchors({"system": "S"}, "claude-opus-4-8") is None


def test_per_event_tokens_differencing(monkeypatch):
    monkeypatch.setenv("COST_XRAY_ANTHROPIC_API_KEY", "sk-test")

    def fake(payload, headers):
        n = 5 + (len(payload.get("system", "")) if "system" in payload else 0)
        n += 10 * len(payload.get("tools", []))
        if payload.get("messages") and payload["messages"] != [{"role": "user", "content": "."}]:
            n += 100
        return n

    req = {"system": "abc", "tools": [{"name": "t1"}, {"name": "t2"}],
           "messages": [{"role": "user", "content": "hello"}]}
    pieces = count_tokens.per_event_tokens(req, "claude-opus-4-8", _http=fake)
    d = dict(pieces)
    assert d["system"] == 3
    assert d["tool:t1"] == 10 and d["tool:t2"] == 10
    assert d["messages"] == 100
    assert sum(t for _, t in pieces) == fake(dict(req), {}) - fake({"messages": count_tokens._STUB}, {})


def test_per_bucket_tokens_leave_one_out(monkeypatch):
    monkeypatch.setenv("COST_XRAY_ANTHROPIC_API_KEY", "sk-test")
    from cost_xray.events import _count_content

    def fake(payload, headers):
        n = 0
        for m in payload["messages"]:
            c = m.get("content")
            if isinstance(c, str):
                n += len(c)
            elif isinstance(c, list):
                for b in c:
                    p = _count_content(b)
                    n += len(p if isinstance(p, str) else "")
        return n

    req = {"messages": [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "AB", "signature": "CD"},
            {"type": "text", "text": "hello"},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "content": "xyz"}]},
    ]}
    out = count_tokens.per_bucket_tokens(req, "claude-opus-4-8", _http=fake)
    assert out["thinking"] == 4 and out["text"] == 5
    assert out["tool_io"] == 2 and out["structure"] == 1
    assert sum(out.values()) == 12


def test_per_message_event_tokens_per_block_with_tool_pairing():
    from cost_xray.events import _count_content

    def fake(payload, headers):
        n = 0
        for m in payload["messages"]:
            c = m.get("content")
            if isinstance(c, str):
                n += len(c)
            elif isinstance(c, list):
                for b in c:
                    p = _count_content(b)
                    n += len(p if isinstance(p, str) else "")
        return n

    req = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "AB", "signature": "CD"},
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "xyz"}]},
    ]}
    out = count_tokens.per_message_event_tokens(req, "claude-opus-4-8", _http=fake)
    by = {(coord, bucket): tok for coord, bucket, tok in out}
    assert len(out) == 4
    assert by[(("msg", 1, 0), "thinking")] == 4
    assert by[(("msg", 1, 1), "text")] == 5
    assert by[(("msg", 0, 0), "text")] == 1
    assert (("tool_io", "t1"), "tool_io") in by
    assert all(tok > 0 for _c, _b, tok in out)


def test_tools_exact_in_context_marginals_persistently_cached_with_ttl(monkeypatch, tmp_path):
    monkeypatch.setattr(count_tokens, "_TOOL_STORE", tmp_path / "tool_tokens.json")
    count_tokens._TOOL_CACHE.clear()
    calls = {"n": 0}

    def fake(payload, headers):
        calls["n"] += 1
        return 5 + 10 * len(payload.get("tools", []))

    tools = [{"name": "a"}, {"name": "b"}]
    assert count_tokens.tools_exact(tools, "claude-opus-4-8", _http=fake, now=1000) == [10, 10]
    n1 = calls["n"]
    assert (tmp_path / "tool_tokens.json").exists()
    count_tokens._TOOL_CACHE.clear()
    assert count_tokens.tools_exact(tools, "claude-opus-4-8", _http=fake, now=1000 + 3600) == [10, 10]
    assert calls["n"] == n1
    count_tokens._TOOL_CACHE.clear()
    assert count_tokens.tools_exact(tools, "claude-opus-4-8", _http=fake, now=1000 + 86400 + 1) == [10, 10]
    assert calls["n"] > n1


def test_input_thinking_tokens_two_call_diff():
    calls = {"n": 0}

    def fake(payload, headers):
        calls["n"] += 1
        n = 0
        for m in payload["messages"]:
            c = m.get("content")
            n += len(c) if isinstance(c, list) else 1
        return n

    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "s"},
                                              {"type": "text", "text": "x"}]}]
    assert count_tokens.input_thinking_tokens(msgs, "claude-opus-4-8", _http=fake) == 1
    calls["n"] = 0
    flat = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
    assert count_tokens.input_thinking_tokens(flat, "claude-opus-4-8", _http=fake) == 0
    assert calls["n"] == 0


def test_output_thinking_tokens_two_call_diff():
    from cost_xray.events import _count_content

    def fake(payload, headers):
        n = 0
        for msg in payload["messages"]:
            c = msg.get("content")
            if isinstance(c, list):
                for b in c:
                    p = _count_content(b)
                    n += len(p) if isinstance(p, str) else 0
        return n

    blocks = [{"type": "thinking", "thinking": "AB", "signature": "CD"},
              {"type": "text", "text": "hello"}]
    assert count_tokens.output_thinking_tokens(blocks, "claude-opus-4-8", _http=fake) == 4
    assert count_tokens.output_thinking_tokens([{"type": "text", "text": "x"}],
                                               "claude-opus-4-8", _http=fake) == 0


def test_auth_headers_prefers_api_key_then_oauth(monkeypatch):
    monkeypatch.setenv("COST_XRAY_ANTHROPIC_API_KEY", "sk-test")
    assert count_tokens.auth_headers()["x-api-key"] == "sk-test"
    monkeypatch.delenv("COST_XRAY_ANTHROPIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(count_tokens, "_oauth_token", lambda: "oauth-tok")
    h = count_tokens.auth_headers()
    assert h["authorization"] == "Bearer oauth-tok" and h["anthropic-beta"] == count_tokens.OAUTH_BETA


def test_auth_headers_oauth_via_config_dir(monkeypatch, tmp_path):
    import json

    from cost_xray import claude_login
    monkeypatch.delenv("COST_XRAY_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    far_future = 9_999_999_999_999
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "live", "expiresAt": far_future}}))
    h = count_tokens.auth_headers()
    assert h["authorization"] == "Bearer live"
    assert claude_login


def test_auth_headers_none_when_login_expired(monkeypatch, tmp_path):
    import json
    monkeypatch.delenv("COST_XRAY_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "stale", "expiresAt": 1}}))
    assert count_tokens.auth_headers() is None


def test_exact_anchors_none_on_http_error(monkeypatch):
    monkeypatch.setenv("COST_XRAY_ANTHROPIC_API_KEY", "sk-test")
    count_tokens._CACHE.clear()

    def boom(payload, headers):
        raise RuntimeError("network down")

    assert count_tokens.exact_anchors({"system": "S", "messages": []}, "claude-opus-4-8",
                                      _http=boom) is None


def test_exact_anchors_caches_by_content(monkeypatch):
    monkeypatch.setenv("COST_XRAY_ANTHROPIC_API_KEY", "sk-test")
    count_tokens._CACHE.clear()
    calls = {"n": 0}

    def counting(payload, headers):
        calls["n"] += 1
        return _fake_counts(payload, headers)

    req = {"system": "S", "tools": [], "messages": [{"role": "user", "content": "hi"}]}
    count_tokens.exact_anchors(req, "claude-opus-4-8", _http=counting)
    n_first = calls["n"]
    count_tokens.exact_anchors(req, "claude-opus-4-8", _http=counting)
    assert calls["n"] == n_first
