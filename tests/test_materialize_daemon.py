"""Materializer one-shot sweep — disk-driven discovery + per-turn materialize, decoupled from the TUI
(docs/design/read-layer.md, ops.md). The capture proxy spawns this module per captured turn; each run
sweeps every session whose raw is newer than its summary, then exits (no poll loop). A per-session
lock serializes concurrent materializes. Framework-free."""
from __future__ import annotations

import fcntl
import json
import multiprocessing as mp

from cost_xray import materialize_daemon as daemon
from cost_xray.materialize import materialize_session


def _write_session(d, content="hello world, list the files please"):
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "request": {
            "model": "claude-opus-4-8",
            "system": "You are helpful.",
            "tools": [{"name": "Bash", "description": "run a shell command"}],
            "messages": [{"role": "user", "content": content}],
        },
        "response": {"streaming": False, "body": {"content": [{"type": "text", "text": "ok"}]}},
        "usage": {"input_tokens": 20, "cache_read_input_tokens": 100,
                  "cache_creation": {"ephemeral_1h_input_tokens": 0}, "output_tokens": 10},
        "status": 200,
    }
    (d / "raw.jsonl").write_text(json.dumps(rec) + "\n")
    return d


def test_stale_until_materialized(tmp_path):
    d = _write_session(tmp_path / "claude" / "s1")
    assert d in set(daemon.stale_sessions(tmp_path))     # raw, no summary → stale
    materialize_session(d)
    assert d not in set(daemon.stale_sessions(tmp_path))  # summary now fresh → not stale


def test_sweep_makes_raw_only_session_discoverable(tmp_path):
    # the discovery fix: a never-opened session gets a summary from the daemon alone, so Home's
    # rollup (built from summaries) will list it — no TUI interaction required.
    d = _write_session(tmp_path / "claude" / "s1")
    assert not (d / "summary.json").exists()
    done = daemon.sweep_once(tmp_path)
    assert d in set(done)
    sm = json.loads((d / "summary.json").read_text())
    assert sm["n_turns"] == 1 and sm["bill"] > 0


def test_sweep_skips_already_fresh(tmp_path):
    _write_session(tmp_path / "claude" / "s1")
    daemon.sweep_once(tmp_path)
    assert daemon.sweep_once(tmp_path) == []             # nothing stale on the second pass


def test_per_session_lock_skips_work_when_held(tmp_path):
    d = _write_session(tmp_path / "claude" / "s1")
    lf = (d / ".materialize.lock").open("w")
    fcntl.flock(lf, fcntl.LOCK_EX)                       # another process is materializing
    try:
        assert materialize_session(d) is None           # contended → skip
        assert not (d / "summary.json").exists()        # no work done while locked
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()
    assert materialize_session(d) is not None           # lock free → materializes
    assert (d / "summary.json").exists()


def test_main_runs_one_sweep_and_exits(tmp_path):
    # the manual CLI is a one-shot: materialize once, return, no loop.
    d = _write_session(tmp_path / "claude" / "s1")
    daemon.main(["--root", str(tmp_path)])              # returns (no perpetual poll to stop)
    assert (d / "summary.json").exists()
    assert not hasattr(daemon, "run")                  # the polling loop is gone


def test_consume_sweeps_on_signal_then_exits_on_eof(tmp_path):
    # the warm consumer: block for a turn signal, sweep every stale session, repeat; exit at EOF
    # (the proxy died). No polling — it does nothing until signalled.
    d = _write_session(tmp_path / "claude" / "s1")
    parent, child = mp.Pipe()
    parent.send(1)                                      # one turn signalled
    parent.close()                                      # then close → consume sweeps once, hits EOF, returns
    daemon.consume(child, root=tmp_path)                # runs in-thread; returns (does not block forever)
    assert (d / "summary.json").exists()                # the signalled turn was materialized
