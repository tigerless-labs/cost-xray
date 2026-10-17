import json

from cost_xray import raw_codec


def test_malformed_entries_do_not_hide_valid_blocks(tmp_path):
    rows = [[[], {}], {}, [], ["short"], ["ok", {"text": "fixture"}]]
    (tmp_path / "blocks.jsonl").write_text("\n".join(map(json.dumps, rows)))
    assert raw_codec.load_store(tmp_path) == {"ok": {"text": "fixture"}}
    assert raw_codec.load_hashes(tmp_path) == {"ok"}
