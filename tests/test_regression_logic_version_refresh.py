import json
import os

from cost_xray import materialize, materialize_daemon


def test_old_logic_is_stale_without_new_traffic(tmp_path):
    d = tmp_path / "claude" / "fixture"
    d.mkdir(parents=True)
    raw = d / "raw.jsonl"
    raw.write_text("{}\n")
    summary = d / "summary.json"
    summary.write_text(json.dumps({"logic_version": materialize.LOGIC_VERSION - 1}))
    os.utime(raw, (1, 1))
    assert materialize_daemon.stale_sessions(tmp_path) == [d]
