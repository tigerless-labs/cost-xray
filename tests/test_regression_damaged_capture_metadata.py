import json

import pytest

from cost_xray import addon, materialize_daemon


@pytest.mark.parametrize("value", [[], {"n_turns": "bad"}, {"n_turns": -4}])
def test_invalid_metadata_recovers_on_capture(tmp_path, value):
    d = tmp_path / "claude" / "fixture"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(value))
    addon._update_meta_info(d, {"ts": 1, "model": "fixture"})
    assert json.loads((d / "meta.json").read_text())["n_turns"] == 1
    assert materialize_daemon.capture_broken_sessions(tmp_path) == [d]
