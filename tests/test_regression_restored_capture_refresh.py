from __future__ import annotations

import json
import os

import pytest

from cost_xray import count_tokens, materialize, materialize_daemon, pricing_map


def _line(label):
    return json.dumps({"request": {"model": "fixture-model", "messages": [
        {"role": "user", "content": label}]},
        "response": {"streaming": False, "body": {"content": [
            {"type": "text", "text": "answer"}]}},
        "usage": {"input_tokens": 20, "output_tokens": 10}, "status": 200}) + "\n"


def _materialized(monkeypatch, tmp_path):
    monkeypatch.setattr(count_tokens, "auth_headers", lambda: None)
    monkeypatch.setattr(pricing_map, "load", lambda: {})
    session = tmp_path / "claude" / "session"
    session.mkdir(parents=True)
    (session / "raw.jsonl").write_text(_line("one") + _line("two"), encoding="utf-8")
    assert materialize.materialize_session(session)["n_turns"] == 2
    return session


@pytest.mark.parametrize("labels", [[], ["one"], ["one", "two", "three"]],
                         ids=["empty-restoration", "truncated-restoration", "appended-restoration"])
def test_restored_capture_size_is_refreshed_with_older_mtime(monkeypatch, tmp_path, labels):
    session = _materialized(monkeypatch, tmp_path)
    raw = session / "raw.jsonl"
    raw.write_text("".join(_line(label) for label in labels), encoding="utf-8")
    summary_stamp = (session / "summary.json").stat().st_mtime_ns
    os.utime(raw, ns=(summary_stamp - 1_000_000_000, summary_stamp - 1_000_000_000))

    assert materialize.needs_refresh(session)
    assert materialize_daemon.sweep_once(tmp_path) == [session]
    summary = json.loads((session / "summary.json").read_text(encoding="utf-8"))
    assert summary["n_turns"] == len(labels)
    assert summary["raw_offset"] == raw.stat().st_size
    assert len((session / "derived.jsonl").read_text(encoding="utf-8").splitlines()) == len(labels)
    assert not materialize.needs_refresh(session)


def test_unchanged_capture_with_older_mtime_stays_fresh(monkeypatch, tmp_path):
    session = _materialized(monkeypatch, tmp_path)
    assert not materialize.needs_refresh(session)
    assert materialize_daemon.sweep_once(tmp_path) == []


def test_incomplete_append_keeps_completed_history_until_newline(monkeypatch, tmp_path):
    session = _materialized(monkeypatch, tmp_path)
    raw = session / "raw.jsonl"
    pending = _line("three")[:-1]
    with raw.open("a", encoding="utf-8") as stream:
        stream.write(pending)
    summary = materialize.materialize_session(session)
    assert summary["n_turns"] == 2
    assert len((session / "derived.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    with raw.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    assert materialize.materialize_session(session)["n_turns"] == 3
    assert not materialize.needs_refresh(session)
