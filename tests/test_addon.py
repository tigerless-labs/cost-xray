from __future__ import annotations

import json

import pytest

from cost_xray import addon


@pytest.mark.parametrize("path,expected", [
    ("/v1/messages", True),
    ("/v1/messages?beta=true", True),
    ("/responses", True),
    ("/v1/chat/completions", True),
    ("/v1/messages/count_tokens", False),
    ("/v1/messages/count_tokens?beta=true", False),
    ("/v1/models", False),
    ("/health", False),
])
def test_matched(make_flow, path, expected):
    flow = make_flow(body={}, path=path)
    assert addon._matched(flow) is expected


def test_redact_headers_hides_secrets_keeps_rest():
    headers = {
        "Authorization": "Bearer sk-secret",
        "x-api-key": "sk-ant-123",
        "anthropic-api-key": "sk-ant-456",
        "Cookie": "session=abc",
        "User-Agent": "claude-cli/1.2.3",
        "content-type": "application/json",
    }
    out = addon._redact_headers(headers)
    assert out["Authorization"] == "<redacted>"
    assert out["x-api-key"] == "<redacted>"
    assert out["anthropic-api-key"] == "<redacted>"
    assert out["Cookie"] == "<redacted>"
    assert out["User-Agent"] == "claude-cli/1.2.3"
    assert out["content-type"] == "application/json"


def test_redact_body_is_recursive():
    body = {
        "model": "claude",
        "api_key": "sk-leak",
        "metadata": {"token": "t0p", "user_id": "u1"},
        "messages": [
            {"role": "user", "content": "hi", "password": "hunter2"},
        ],
    }
    out = addon._redact_body(body)
    assert out["api_key"] == "<redacted>"
    assert out["metadata"]["token"] == "<redacted>"
    assert out["metadata"]["user_id"] == "u1"
    assert out["messages"][0]["password"] == "<redacted>"
    assert out["messages"][0]["content"] == "hi"
    assert body["api_key"] == "sk-leak"


@pytest.mark.parametrize("ua,agent", [
    ("claude-cli/1.0", "claude"),
    ("Claude Code", "claude"),
    ("codex/0.3", "codex"),
    ("Cursor/2.0", "cursor"),
    ("python-requests/2.0", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_agent_of(ua, agent):
    assert addon._agent_of(ua) == agent


def test_session_id_from_header(make_flow):
    flow = make_flow(body={}, req_headers={"x-claude-code-session-id": "sess-from-header"})
    assert addon._session_id(flow, {}) == "sess-from-header"


def test_session_id_from_metadata_user_id(make_flow):
    flow = make_flow(body={})
    body = {"metadata": {"user_id": json.dumps({"session_id": "sess-from-meta"})}}
    assert addon._session_id(flow, body) == "sess-from-meta"


def test_session_id_from_codex_prompt_cache_key(make_flow):
    flow = make_flow(body={}, conn_id="aaaa1111bbbb2222")
    frame = {"type": "response.create", "prompt_cache_key": "019e985d-f1ae-72c0-814f-5f059996f3d9"}
    assert addon._session_id(flow, frame) == "019e985d-f1ae-72c0-814f-5f059996f3d9"
    meta = {"client_metadata": {"x-codex-turn-metadata": json.dumps({"session_id": "sess-codex"})}}
    assert addon._session_id(flow, meta) == "sess-codex"


def test_session_id_falls_back_to_conn_id(make_flow):
    flow = make_flow(body={}, conn_id="0123456789abcdef")
    sid = addon._session_id(flow, {})
    assert sid == "conn-01234567"


def test_parse_sse_collects_events_and_merges_usage():
    sse = "\n".join([
        'event: message_start',
        'data: {"type":"message_start","message":{"usage":{"input_tokens":100}}}',
        '',
        'data: {"type":"content_block_delta","delta":{"text":"hi"}}',
        '',
        'data: {"type":"message_delta","usage":{"output_tokens":50}}',
        '',
        'data: [DONE]',
    ])
    events, usage = addon._parse_sse(sse)
    assert len(events) == 3
    assert usage == {"input_tokens": 100, "output_tokens": 50}


def test_parse_sse_handles_garbage_lines():
    sse = "data: not-json\n\ndata: {\"ok\":true}\n"
    events, usage = addon._parse_sse(sse)
    assert events == [{"ok": True}]
    assert usage is None


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(addon, "SESSIONS", sessions)
    return sessions


@pytest.fixture(autouse=True)
def captured_kicks(monkeypatch):
    sent = []

    class _RecorderConn:
        def send(self, v):
            sent.append(v)

    monkeypatch.setattr(addon, "_ensure_consumer", lambda: _RecorderConn())
    return sent


def test_request_writes_meta_not_current(make_flow, isolated_sessions):
    body = {"model": "claude-opus-4-8", "system": "hi", "api_key": "sk-leak"}
    flow = make_flow(
        body=body,
        req_headers={"user-agent": "claude-cli/1.0", "x-claude-code-session-id": "S1"},
    )
    addon.request(flow)

    d = isolated_sessions / "claude" / "S1"
    assert d.is_dir()
    assert not (d / "current.json").exists()
    meta = json.loads((d / "meta.json").read_text())
    assert meta.get("model") == "claude-opus-4-8"

    meta = json.loads((d / "meta.json").read_text())
    assert meta["agent"] == "claude"
    assert meta["model"] == "claude-opus-4-8"
    assert meta["n_turns"] == 1


def test_meta_n_turns_increments_per_request(make_flow, isolated_sessions):
    body = {"model": "claude-opus-4-8"}
    headers = {"user-agent": "claude-cli/1.0", "x-claude-code-session-id": "S2"}
    for _ in range(3):
        addon.request(make_flow(body=body, req_headers=headers))
    meta = json.loads((isolated_sessions / "claude" / "S2" / "meta.json").read_text())
    assert meta["n_turns"] == 3


def test_response_appends_redacted_turn_to_raw_jsonl(make_flow, isolated_sessions):
    body = {"model": "claude-opus-4-8", "api_key": "sk-leak"}
    sse = "\n".join([
        'data: {"type":"message_start","message":{"usage":{"input_tokens":12}}}',
        '',
        'data: {"type":"message_delta","usage":{"output_tokens":7}}',
    ])
    flow = make_flow(
        body=body,
        req_headers={
            "user-agent": "claude-cli/1.0",
            "x-claude-code-session-id": "S3",
            "authorization": "Bearer sk-secret",
        },
        resp_text=sse,
        resp_headers={"content-type": "text/event-stream"},
    )
    addon.response(flow)

    raw = isolated_sessions / "claude" / "S3" / "raw.jsonl"
    lines = [ln for ln in raw.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])

    assert rec["status"] == 200
    assert rec["model"] == "claude-opus-4-8"
    assert rec["request"]["api_key"] == "<redacted>"
    assert rec["request_headers"]["authorization"] == "<redacted>"
    assert rec["response"]["streaming"] is True
    assert rec["usage"] == {"input_tokens": 12, "output_tokens": 7}


def test_response_handles_non_streaming_json(make_flow, isolated_sessions):
    body = {"model": "claude-opus-4-8"}
    resp = {"usage": {"input_tokens": 5}, "content": [{"type": "text", "text": "ok"}]}
    flow = make_flow(
        body=body,
        req_headers={"user-agent": "claude-cli/1.0", "x-claude-code-session-id": "S4"},
        resp_text=json.dumps(resp),
        resp_headers={"content-type": "application/json"},
    )
    addon.response(flow)

    rec = json.loads((isolated_sessions / "claude" / "S4" / "raw.jsonl").read_text().strip())
    assert rec["response"]["streaming"] is False
    assert rec["response"]["body"]["content"][0]["text"] == "ok"
    assert rec["usage"] == {"input_tokens": 5}


class _WsMessage:
    def __init__(self, obj, *, from_client, ts=1_700_000_000.0):
        self.content = json.dumps(obj).encode()
        self.from_client = from_client
        self.timestamp = ts


class _Ws:
    def __init__(self):
        self.messages = []


def test_websocket_message_appends_raw_realtime(make_flow, isolated_sessions):
    flow = make_flow(
        body=None,
        path="/backend-api/codex/responses",
        req_headers={"user-agent": "codex/0.1"},
        conn_id="feedfacecafebeef",
    )
    flow.websocket = _Ws()

    create = {"type": "response.create", "model": "gpt-5.5",
              "instructions": "sys", "tools": [], "input": []}
    flow.websocket.messages.append(_WsMessage(create, from_client=True))
    addon.websocket_message(flow)
    addon._drain_writes_for_tests()

    d = isolated_sessions / "codex" / "conn-feedface"
    raw = d / "raw.jsonl"
    lines = raw.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "response.create"
    meta = json.loads((d / "meta.json").read_text())
    assert meta.get("model") == "gpt-5.5"

    completed = {"type": "response.completed",
                 "response": {"usage": {"input_tokens": 1, "output_tokens": 0}}}
    flow.websocket.messages.append(_WsMessage(completed, from_client=False, ts=1_700_000_001.0))
    addon.websocket_message(flow)
    addon._drain_writes_for_tests()

    lines = raw.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["type"] == "response.completed"


def test_non_matching_path_is_ignored(make_flow, isolated_sessions):
    flow = make_flow(body={"model": "x"}, path="/v1/models",
                     req_headers={"user-agent": "claude-cli/1.0"})
    addon.request(flow)
    assert list(isolated_sessions.iterdir()) == []


def test_response_signals_materialize(make_flow, isolated_sessions, captured_kicks):
    flow = make_flow(
        body={"model": "claude-opus-4-8"},
        req_headers={"user-agent": "claude-cli/1.0", "x-claude-code-session-id": "S6"},
        resp_text=json.dumps({"usage": {"input_tokens": 5}, "content": []}),
        resp_headers={"content-type": "application/json"},
    )
    addon.response(flow)
    addon._drain_writes_for_tests()

    assert len(captured_kicks) == 1


def test_ws_signals_only_on_response_completed(make_flow, isolated_sessions, captured_kicks):
    flow = make_flow(body=None, path="/backend-api/codex/responses",
                     req_headers={"user-agent": "codex/0.1"}, conn_id="feedfacecafebeef")
    flow.websocket = _Ws()

    inter = {"type": "response.output_item.done", "item": {"type": "message"}}
    flow.websocket.messages.append(_WsMessage(inter, from_client=False))
    addon.websocket_message(flow)
    addon._drain_writes_for_tests()
    assert captured_kicks == []

    completed = {"type": "response.completed",
                 "response": {"usage": {"input_tokens": 1, "output_tokens": 0}}}
    flow.websocket.messages.append(_WsMessage(completed, from_client=False, ts=1_700_000_001.0))
    addon.websocket_message(flow)
    addon._drain_writes_for_tests()
    assert len(captured_kicks) == 1


def test_count_tokens_calls_are_relayed_not_recorded(make_flow, isolated_sessions):
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]}
    flow = make_flow(
        body=body,
        path="/v1/messages/count_tokens?beta=true",
        req_headers={"user-agent": "claude-cli/1.0", "x-claude-code-session-id": "SCT"},
        resp_text=json.dumps({"input_tokens": 5}),
        resp_headers={"content-type": "application/json"},
    )
    addon.request(flow)
    addon.response(flow)
    addon._drain_writes_for_tests()
    assert not (isolated_sessions / "claude" / "SCT").exists()


def test_raw_write_failure_is_logged_not_silent(make_flow, isolated_sessions, monkeypatch, caplog):
    import logging

    def boom(*a, **k):
        raise OSError("disk says no")

    monkeypatch.setattr(addon.raw_codec, "append_record", boom)
    flow = make_flow(
        body={"model": "claude-opus-4-8"},
        req_headers={"user-agent": "claude-cli/1.0", "x-claude-code-session-id": "SLOG"},
        resp_text=json.dumps({"usage": {"input_tokens": 5}, "content": []}),
        resp_headers={"content-type": "application/json"},
    )
    with caplog.at_level(logging.WARNING):
        addon.response(flow)
    assert "raw write failed" in caplog.text
    assert "SLOG" in caplog.text
    assert not (isolated_sessions / "claude" / "SLOG" / "raw.jsonl").exists()
