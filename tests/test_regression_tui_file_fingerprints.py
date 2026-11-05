import os

from cost_xray import tui


def test_summary_atomic_replacement_invalidates_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "_ensure_fresh", lambda d: None)
    tui._SUMMARY_CACHE.clear()
    p = tmp_path / "summary.json"
    p.write_text('{"value":1}')
    stat = p.stat()
    assert tui._summary(tmp_path) == {"value": 1}
    replacement = tmp_path / "replacement"
    replacement.write_text('{"value":2}')
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(p)
    assert tui._summary(tmp_path) == {"value": 2}


def test_fresh_summary_check_retains_timestamp_comparison(tmp_path):
    raw = tmp_path / "raw.jsonl"
    raw.write_text("{}\n")
    summary = tmp_path / "summary.json"
    summary.write_text("{}")
    tui._ensure_fresh(tmp_path)
