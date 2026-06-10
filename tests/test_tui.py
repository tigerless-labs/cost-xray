from __future__ import annotations

import json

from cost_xray import tui


def test_h_small_numbers():
    assert tui._h(0) == "0"
    assert tui._h(42) == "42"
    assert tui._h(999) == "999"


def test_h_thousands_and_millions():
    assert tui._h(1000) == "1.0k"
    assert tui._h(12_500) == "12.5k"
    assert tui._h(1_000_000) == "1.0M"
    assert tui._h(2_300_000) == "2.3M"


def test_bar_fill_is_proportional():
    full = tui._bar(1.0, width=10)
    empty = tui._bar(0.0, width=10)
    half = tui._bar(0.5, width=10)
    assert full.plain == "█" * 10
    assert empty.plain == "·" * 10
    assert half.plain == "█" * 5 + "·" * 5


def test_bar_clamps_out_of_range_fractions():
    assert tui._bar(5.0, width=8).plain == "█" * 8
    assert tui._bar(-1.0, width=8).plain == "·" * 8


def _session_with_request(tmp_path, body):
    d = tmp_path / "claude" / "sess"
    d.mkdir(parents=True)
    (d / "raw.jsonl").write_text(json.dumps({"request": body}) + "\n")
    return d


def test_latest_request_reads_last_raw_jsonl_line(tmp_path):
    d = tmp_path / "claude" / "sess"
    d.mkdir(parents=True)
    with (d / "raw.jsonl").open("w") as f:
        f.write(json.dumps({"request": {"model": "old"}}) + "\n")
        f.write(json.dumps({"request": {"model": "newest"}}) + "\n")
    assert tui._latest_request(d) == {"model": "newest"}


def test_report_for_runs_analysis_on_latest_request(tmp_path):
    body = {
        "model": "claude-opus-4-8",
        "system": "hi",
        "tools": [{"name": "mcp__slack__post"}],
        "messages": [{"role": "user", "content": "go"}],
    }
    d = _session_with_request(tmp_path, body)
    r = tui._report_for(d)
    assert r is not None
    assert r["model"] == "claude-opus-4-8"
    assert [s["server"] for s in r["savings"]["unused_mcp_servers"]] == ["slack"]


def test_report_for_returns_none_when_nothing_captured(tmp_path):
    d = tmp_path / "claude" / "empty"
    d.mkdir(parents=True)
    assert tui._report_for(d) is None


def test_sessions_lists_and_sorts_by_activity(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "ROOT", tmp_path)
    older = tmp_path / "claude" / "old"
    newer = tmp_path / "claude" / "new"
    for d in (older, newer):
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"n_turns": 1}))
    import os
    os.utime(older / "meta.json", (1_000, 1_000))
    os.utime(newer / "meta.json", (2_000, 2_000))

    sessions = tui._sessions()
    assert [s["sid"] for s in sessions] == ["new", "old"]
    assert all(s["agent"] == "claude" for s in sessions)


def test_sessions_empty_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "ROOT", tmp_path / "does-not-exist")
    assert tui._sessions() == []
