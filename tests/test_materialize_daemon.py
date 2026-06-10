from __future__ import annotations

import fcntl
import json
import os

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
    assert d in set(daemon.stale_sessions(tmp_path))
    materialize_session(d)
    assert d not in set(daemon.stale_sessions(tmp_path))


def test_sweep_makes_raw_only_session_discoverable(tmp_path):
    d = _write_session(tmp_path / "claude" / "s1")
    assert not (d / "summary.json").exists()
    done = daemon.sweep_once(tmp_path)
    assert d in set(done)
    sm = json.loads((d / "summary.json").read_text())
    assert sm["n_turns"] == 1 and sm["bill"] > 0


def test_sweep_skips_already_fresh(tmp_path):
    _write_session(tmp_path / "claude" / "s1")
    daemon.sweep_once(tmp_path)
    assert daemon.sweep_once(tmp_path) == []


def test_per_session_lock_skips_work_when_held(tmp_path):
    d = _write_session(tmp_path / "claude" / "s1")
    lf = (d / ".materialize.lock").open("w")
    fcntl.flock(lf, fcntl.LOCK_EX)
    try:
        assert materialize_session(d) is None
        assert not (d / "summary.json").exists()
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()
    assert materialize_session(d) is not None
    assert (d / "summary.json").exists()


def test_main_runs_one_sweep_and_exits(tmp_path):
    d = _write_session(tmp_path / "claude" / "s1")
    daemon.main(["--root", str(tmp_path)])
    assert (d / "summary.json").exists()
    assert not hasattr(daemon, "run")


def test_watch_sweeps_on_signal_then_exits_on_eof(tmp_path):
    d = _write_session(tmp_path / "claude" / "s1")
    r, w = os.pipe()
    os.write(w, b"\n")
    os.close(w)
    with os.fdopen(r, "rb", buffering=0) as reader:
        daemon.watch(reader, root=tmp_path)
    assert (d / "summary.json").exists()


def _write_broken(agent_dir, sid, n_turns=3):
    d = agent_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(
        {"session_id": sid, "agent": agent_dir.name, "n_turns": n_turns}))
    return d


def test_sweep_flags_capture_broken_sessions_in_rollup(tmp_path):
    agent = tmp_path / "claude"
    _write_broken(agent, "s-broken")
    _write_session(agent / "s-ok")
    daemon.sweep_once(tmp_path)
    roll = json.loads((agent / "_rollup.json").read_text())
    assert roll["broken"] == ["s-broken"]


def test_sweep_clears_broken_flag_once_raw_appears(tmp_path):
    agent = tmp_path / "claude"
    d = _write_broken(agent, "s-heals")
    daemon.sweep_once(tmp_path)
    assert json.loads((agent / "_rollup.json").read_text())["broken"] == ["s-heals"]
    _write_session(d)
    daemon.sweep_once(tmp_path)
    assert json.loads((agent / "_rollup.json").read_text())["broken"] == []


def test_zero_turn_meta_is_not_flagged(tmp_path):
    agent = tmp_path / "claude"
    _write_broken(agent, "s-empty", n_turns=0)
    _write_session(agent / "s-ok")
    daemon.sweep_once(tmp_path)
    assert json.loads((agent / "_rollup.json").read_text())["broken"] == []
