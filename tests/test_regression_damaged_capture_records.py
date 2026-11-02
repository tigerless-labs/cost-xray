import json

import pytest

from cost_xray import raw_codec


@pytest.mark.parametrize(
    "tail", ["{torn", json.dumps({"_fmt": "d1", "request": {"tools": {"$t": "missing"}}})]
)
def test_unreadable_records_do_not_hide_valid_capture(tmp_path, tail):
    good = {"request": {"messages": []}, "usage": {"input_tokens": 1}}
    (tmp_path / "raw.jsonl").write_text(
        json.dumps(good) + "\n" + tail + "\n" + json.dumps(good) + "\n"
    )
    assert list(raw_codec.iter_records(tmp_path)) == [good, good]
    (tmp_path / "raw.jsonl").write_text(json.dumps(good) + "\n" + tail)
    assert raw_codec.latest_record(tmp_path) == good
