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


def test_ensure_fresh_caps_concurrent_materialize(tmp_path, monkeypatch):
    import threading

    gate = threading.Event()
    started = []

    def blocking_materialize(d):
        started.append(d)
        gate.wait(5)

    monkeypatch.setattr(tui, "materialize_session", blocking_materialize)
    monkeypatch.setattr(tui, "_MAT_THREADS", {})
    monkeypatch.setattr(tui, "_MAT_CAP", 2)

    dirs = []
    for i in range(6):
        d = tmp_path / f"s{i}"
        d.mkdir()
        (d / "raw.jsonl").write_text("x")
        dirs.append(d)

    try:
        for d in dirs:
            tui._ensure_fresh(d)
        alive = sum(1 for t in tui._MAT_THREADS.values() if t.is_alive())
        assert alive <= 2
        assert len(started) <= 2
    finally:
        gate.set()
        for t in list(tui._MAT_THREADS.values()):
            t.join(5)


def test_latest_main_derived_picks_compacted_mainline_over_sidecalls(tmp_path):
    d = tmp_path / "claude" / "sess"
    d.mkdir(parents=True)

    def is_static(e):
        return e.get("section") == "static"

    def scaffold():
        return [{"zone": "input", "section": "static", "bucket": "system", "tokens": 100},
                {"zone": "input", "section": "static", "bucket": "schema", "tool": "Agent",
                 "tokens": 300}]

    def msgs(n):
        return [{"zone": "input", "section": "messages", "bucket": "text", "role": "user",
                 "tokens": 50} for _ in range(n)]

    lines = [
        {"turn": 1, "window": 1000, "events": scaffold() + msgs(10)},
        {"turn": 2, "window": 1000, "events": scaffold() + msgs(20)},
        {"turn": 3, "window": 1000, "events": scaffold() + msgs(1)},
        {"turn": 4, "window": 1000, "events":
            [{"zone": "input", "section": "static", "bucket": "system", "tokens": 20},
             {"zone": "input", "section": "messages", "bucket": "text", "role": "user",
              "tokens": 30}]},
    ]
    with (d / "derived.jsonl").open("w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")

    def total(line):
        return sum(e["tokens"] for e in line["events"] if e["zone"] == "input")

    assert tui._tail_records(d / "derived.jsonl")[-1]["turn"] == 4
    picked = tui._latest_main_derived(d, is_static)
    assert picked["turn"] == 3
    assert total(picked) < total(lines[1])
    assert total(picked) > total(lines[3])


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
