from __future__ import annotations

import json

from cost_xray import raw_codec as rc


def _turn(messages, *, tools=None, system=None, model="claude-opus-4-8", ts=1.0):
    req = {"model": model, "messages": messages}
    if system is not None:
        req["system"] = system
    if tools is not None:
        req["tools"] = tools
    return {
        "ts": ts, "host": "api.anthropic.com", "path": "/v1/messages?beta=true",
        "model": model, "status": 200, "request_headers": {"x-api-key": "<redacted>"},
        "request": req,
        "response": {"streaming": False, "body": {"content": [{"type": "text", "text": "ok"}]}},
        "usage": {"input_tokens": 5, "cache_read_input_tokens": 0, "output_tokens": 2},
    }


def _msg(i):
    return {"role": "user" if i % 2 == 0 else "assistant",
            "content": [{"type": "text", "text": f"message number {i} " + "x" * 40}]}


def test_encode_decode_is_byte_identical():
    rec = _turn([_msg(0), _msg(1), _msg(2)], tools=[{"name": "Bash"}], system="be brief")
    delta, blocks = rc.encode(rec)
    assert delta is not rec and delta["_fmt"] == rc._FMT
    assert rc._dump(rc.decode(delta, blocks)) == rc._dump(rec)


def test_append_then_iter_round_trips_every_turn(tmp_path):
    turns = [_turn([_msg(i) for i in range(k)], tools=[{"name": "Bash"}], system="sys")
             for k in range(1, 8)]
    for t in turns:
        rc.append_record(tmp_path, t)
    back = list(rc.iter_records(tmp_path))
    assert len(back) == len(turns)
    for got, want in zip(back, turns, strict=True):
        assert rc._dump(got) == rc._dump(want)


def test_dedup_stores_each_unique_block_once(tmp_path):
    n = 12
    turns = [_turn([_msg(i) for i in range(k)]) for k in range(1, n + 1)]
    total_slots = sum(len(t["request"]["messages"]) for t in turns)
    for t in turns:
        rc.append_record(tmp_path, t)
    store = rc.load_store(tmp_path)
    msg_blocks = [v for v in store.values() if isinstance(v, dict)]
    assert len(msg_blocks) < total_slots
    assert len(msg_blocks) == n
    assert total_slots > len(msg_blocks) * 3


def test_compaction_shrinks_total_bytes(tmp_path):
    turns = [_turn([_msg(i) for i in range(k)], tools=[{"name": "Bash"}]) for k in range(1, 20)]
    naive = sum(len(rc._dump(t)) + 1 for t in turns)
    for t in turns:
        rc.append_record(tmp_path, t)
    deduped = (tmp_path / rc.RAW).stat().st_size + (tmp_path / rc.BLOCKS).stat().st_size
    assert deduped < naive


def test_legacy_whole_body_passthrough(tmp_path):
    legacy = _turn([_msg(0), _msg(1)])
    (tmp_path / rc.RAW).write_text(rc._dump(legacy) + "\n")
    back = list(rc.iter_records(tmp_path))
    assert len(back) == 1 and rc._dump(back[0]) == rc._dump(legacy)
    assert rc._dump(rc.latest_record(tmp_path)) == rc._dump(legacy)


def test_legacy_and_delta_coexist_in_one_log(tmp_path):
    legacy = _turn([_msg(0)])
    (tmp_path / rc.RAW).write_text(rc._dump(legacy) + "\n")
    rc.append_record(tmp_path, _turn([_msg(0), _msg(1)]))
    back = list(rc.iter_records(tmp_path))
    assert len(back) == 2
    assert rc._dump(back[0]) == rc._dump(legacy)
    assert back[1]["request"]["messages"][1]["content"][0]["text"].startswith("message number 1")


def test_latest_record_reconstructs_last_turn(tmp_path):
    for k in range(1, 5):
        rc.append_record(tmp_path, _turn([_msg(i) for i in range(k)]))
    last = rc.latest_record(tmp_path)
    assert len(last["request"]["messages"]) == 4


def test_non_dict_request_passes_through_encode():
    frame = {"ts": 1.0, "transport": "websocket", "type": "response.completed", "frame": {"x": 1}}
    delta, blocks = rc.encode(frame)
    assert delta is frame and blocks == {}


def _append_growing(d, n, ctx):
    for k in range(1, n + 1):
        rc.append_record(d, _turn([_msg(i) for i in range(k)]), ctx=ctx)


def _total_bytes(d):
    return sum((d / f).stat().st_size for f in (rc.RAW, rc.BLOCKS) if (d / f).exists())


def test_ref_list_delta_decodes_statelessly_per_record(tmp_path):
    ctx = {}
    _append_growing(tmp_path, 30, ctx)
    lines = [json.loads(ln) for ln in (tmp_path / rc.RAW).read_text().splitlines()]
    slots = [ln["request"]["messages"] for ln in lines]
    assert any(rc._REF_MK in s for s in slots)
    assert any(rc._REF_MD in s for s in slots)
    store = rc.load_store(tmp_path)
    for ln, want_k in zip(reversed(lines), range(30, 0, -1), strict=True):
        full = rc.decode(ln, store)
        assert rc._dump(full["request"]["messages"]) == rc._dump([_msg(i) for i in range(want_k)])


def _grow_dir(parent, name, n, ctx):
    d = parent / name
    d.mkdir()
    _append_growing(d, n, ctx)
    return d


def test_ref_list_delta_is_subquadratic_and_lossless(tmp_path):
    a = _grow_dir(tmp_path, "a", 30, {})
    a2 = _grow_dir(tmp_path, "a2", 60, {})
    base = _grow_dir(tmp_path, "base", 30, None)
    assert _total_bytes(a2) < 3 * _total_bytes(a)
    assert _total_bytes(a) < _total_bytes(base)
    back = list(rc.iter_records(a))
    want = [_turn([_msg(i) for i in range(k)]) for k in range(1, 31)]
    assert all(rc._dump(x) == rc._dump(y) for x, y in zip(back, want, strict=True))


def test_keyframe_reemitted_after_history_rewrite(tmp_path):
    ctx = {}
    _append_growing(tmp_path, 10, ctx)
    summary = {"role": "user", "content": [{"type": "text", "text": "compacted summary"}]}
    rc.append_record(tmp_path, _turn([summary]), ctx=ctx)
    rc.append_record(tmp_path, _turn([summary, _msg(99)]), ctx=ctx)
    lines = [json.loads(ln) for ln in (tmp_path / rc.RAW).read_text().splitlines()]
    assert rc._REF_MK in lines[-2]["request"]["messages"]
    store = rc.load_store(tmp_path)
    got = rc.decode(lines[-1], store)["request"]["messages"]
    assert got[0]["content"][0]["text"] == "compacted summary"
    assert len(list(rc.iter_records(tmp_path))) == 12


def test_legacy_full_ref_list_delta_still_decodes(tmp_path):
    m0, m1 = _msg(0), _msg(1)
    h0, h1 = rc._hash(m0), rc._hash(m1)
    (tmp_path / rc.BLOCKS).write_text(rc._dump([h0, m0]) + "\n" + rc._dump([h1, m1]) + "\n")
    rec = {"ts": 1.0, "request": {"model": "m", "messages": {rc._REF_MSGS: [h0, h1]}}, "_fmt": rc._FMT}
    out = rc.decode(rec, rc.load_store(tmp_path))
    assert rc._dump(out["request"]["messages"]) == rc._dump([m0, m1])


def test_addon_write_http_record_dedups_and_round_trips(tmp_path):
    from cost_xray import addon
    addon._BLOCK_SEEN.clear()
    addon._BLOCK_CTX.clear()
    t1 = _turn([_msg(0), _msg(1)])
    t2 = _turn([_msg(0), _msg(1), _msg(2)])
    addon._write_http_record(tmp_path, t1)
    addon._write_http_record(tmp_path, t2)
    raw_lines = (tmp_path / rc.RAW).read_text().splitlines()
    assert len(raw_lines) == 2
    assert all(json.loads(ln).get("_fmt") == rc._FMT for ln in raw_lines)
    msg_blocks = [v for v in rc.load_store(tmp_path).values() if isinstance(v, dict)]
    assert len(msg_blocks) == 3
    back = list(rc.iter_records(tmp_path))
    assert rc._dump(back[0]) == rc._dump(t1) and rc._dump(back[1]) == rc._dump(t2)


def _anthropic_turn(messages):
    return {
        "ts": 1.0, "host": "api.anthropic.com", "path": "/v1/messages",
        "model": "claude-opus-4-8", "status": 200, "request_headers": {},
        "request": {"model": "claude-opus-4-8", "messages": messages},
        "response": {"streaming": False, "body": {"content": [{"type": "text", "text": "ok"}]}},
        "usage": {"input_tokens": 50, "cache_read_input_tokens": 300,
                  "cache_creation": {"ephemeral_1h_input_tokens": 0}, "output_tokens": 10},
    }


def test_materialize_reads_deduped_same_as_legacy(tmp_path):
    from cost_xray.materialize import materialize_session
    recs = [_anthropic_turn([_msg(i) for i in range(k)]) for k in range(1, 4)]
    legacy = tmp_path / "claude" / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "raw.jsonl").write_text("".join(rc._dump(r) + "\n" for r in recs))
    deduped = tmp_path / "claude" / "deduped"
    deduped.mkdir(parents=True)
    for r in recs:
        rc.append_record(deduped, r)
    sl = materialize_session(legacy)
    sd = materialize_session(deduped)
    assert sl["n_turns"] == sd["n_turns"] >= 1
    assert sl["bill"] == sd["bill"]
    assert sl.get("tokens") == sd.get("tokens")
