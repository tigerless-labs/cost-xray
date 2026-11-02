from cost_xray import tui


def test_nonobject_derived_rows_do_not_hide_valid_turn(tmp_path):
    path = tmp_path / "derived.jsonl"
    path.write_text('{"turn":1,"events":[]}\nnull\n[]\n')
    assert tui._latest_derived(tmp_path) == {"turn": 1, "events": []}
    assert tui._latest_main_derived(tmp_path, lambda e: False) == {"turn": 1, "events": []}
