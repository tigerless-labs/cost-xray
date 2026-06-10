from __future__ import annotations

import json

import pytest

from cost_xray import drill
from cost_xray.materialize import materialize_session


def _session(tmp_path):
    d = tmp_path / "claude" / "sess"
    d.mkdir(parents=True)
    rec = {
        "request": {
            "model": "claude-opus-4-8",
            "system": "You are helpful.",
            "tools": [{"name": "Bash", "description": "run"}],
            "messages": [
                {"role": "user", "content": "list files"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"cmd": "ls"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "alpha.py beta.py"}]},
            ],
        },
        "response": {"streaming": False, "body": {"content": [{"type": "text", "text": "done"}]}},
        "usage": {"input_tokens": 20, "cache_read_input_tokens": 100,
                  "cache_creation": {"ephemeral_1h_input_tokens": 0}, "output_tokens": 1000},
        "status": 200,
    }
    (d / "raw.jsonl").write_text(json.dumps(rec) + "\n")
    materialize_session(d)
    return d


def test_bucket_breakdown_groups_tool_result_by_producing_tool(tmp_path):
    d = _session(tmp_path)
    rows = drill.bucket_breakdown(d, "input", "messages", "tool_result")
    assert [r["label"] for r in rows] == ["Bash"]
    assert rows[0]["n"] == 1 and rows[0]["tokens"] > 0


def test_tool_calls_lists_individual_events_with_refs(tmp_path):
    d = _session(tmp_path)
    calls = drill.tool_calls(d, "input", "messages", "tool_result", "Bash")
    assert len(calls) == 1
    assert calls[0]["ref"] == {"turn": 0, "msg": 2, "block": 0}


def test_fetch_content_lazily_returns_the_real_output(tmp_path):
    d = _session(tmp_path)
    ref = drill.tool_calls(d, "input", "messages", "tool_result", "Bash")[0]["ref"]
    assert drill.fetch_content(d, ref) == "alpha.py beta.py"


def test_fetch_content_handles_system_and_missing_gracefully(tmp_path):
    d = _session(tmp_path)
    assert "helpful" in drill.fetch_content(d, {"turn": 0, "field": "system"})
    assert drill.fetch_content(d, {"turn": 99, "msg": 0, "block": 0}) == ""
    assert drill.fetch_content(d, None) == ""


def test_cost_drill_reads_per_tool_from_summary_not_derived(tmp_path):
    import json as _json

    d = _session(tmp_path)
    sm = _json.loads((d / "summary.json").read_text())
    g, lbl = "Messages", "system tool use+output"
    rows = drill.cat_breakdown(d, g, lbl)
    assert [r["label"] for r in rows] == ["Bash"]
    cell = sm["by_category"][f"{g}|{lbl}"]
    assert rows[0]["usd"] == pytest.approx(cell["usd"])
    assert sum(r.get("cached_usd", 0) for r in rows) == pytest.approx(cell["cached_usd"])
    assert drill.cat_calls(d, g, lbl, "Bash")[0]["ref"] is not None


def test_skill_loads_and_ads_drill_per_skill(tmp_path):
    import json as _json

    from cost_xray import tui
    d = tmp_path / "claude" / "sk"
    d.mkdir(parents=True)
    rec = {
        "request": {
            "model": "claude-opus-4-8",
            "tools": [{"name": "Skill", "description": "execute a skill"}],
            "messages": [
                {"role": "system", "content":
                    "The following skills are available for use with the Skill tool:\n"
                    "- ascii-banner: render a word as a big ASCII banner\n"
                    "- json-sort-keys: sort json keys recursively\n"},
                {"role": "user", "content": "make a banner"},
                {"role": "user", "content": [{"type": "text", "text":
                    "Base directory for this skill: /x/skills/ascii-banner\n# ASCII Banner\nbody one two three"}]},
            ],
        },
        "response": {"streaming": False, "body": {"content": [{"type": "text", "text": "ok"}]}},
        "usage": {"input_tokens": 50, "cache_read_input_tokens": 200,
                  "cache_creation": {"ephemeral_1h_input_tokens": 0}, "output_tokens": 10},
        "status": 200,
    }
    (d / "raw.jsonl").write_text(_json.dumps(rec) + "\n")
    materialize_session(d)
    sm = _json.loads((d / "summary.json").read_text())

    ads = drill.cat_breakdown(d, "Static", "Skills")
    assert {"ascii-banner", "json-sort-keys"} <= {r["label"] for r in ads}
    assert sum(r["usd"] for r in ads) == pytest.approx(sm["by_category"]["Static|Skills"]["usd"])

    loads = drill.cat_breakdown(d, "Messages", "Skill loads")
    assert [r["label"] for r in loads] == ["ascii-banner"]
    assert loads[0]["usd"] == pytest.approx(sm["by_category"]["Messages|Skill loads"]["usd"])

    events = [e for e in (tui._latest_derived(d) or {}).get("events", []) if e["zone"] == "input"]
    ctx = drill.ctx_breakdown(events, "Messages", "Skill loads")
    assert [r["label"] for r in ctx] == ["ascii-banner"]


def test_refold_from_derived_matches_full_build(tmp_path):
    from cost_xray import materialize as M

    d = _session(tmp_path)
    full = json.loads((d / "summary.json").read_text())
    refold = M._refold(d, "claude", {"raw_offset": full.get("raw_offset", 0), "name": full.get("name")})
    assert refold["n_turns"] == full["n_turns"]
    assert set(refold["by_category"]) == set(full["by_category"])
    for k, v in full["by_category"].items():
        assert refold["by_category"][k]["usd"] == pytest.approx(v["usd"])
    assert refold["bill"] == pytest.approx(full["bill"])
    assert set(refold["by_cat_tool"]) == set(full["by_cat_tool"])


def test_rollup_auto_updates_project_totals_on_materialize(tmp_path):
    import json as _json

    from cost_xray import materialize as M

    d = _session(tmp_path)
    sm = _json.loads((d / "summary.json").read_text())
    rp = d.parent / "_rollup.json"
    assert rp.exists()
    roll = _json.loads(rp.read_text())
    sid = d.name
    assert sid in roll["sessions"]
    proj = roll["sessions"][sid]["project"] or "—"
    assert roll["projects"][proj]["bill"] == pytest.approx(sm["bill"])
    assert roll["totals"]["bill"] == pytest.approx(sm["bill"])
    assert roll["totals"]["n_sessions"] == 1
    M.materialize_session(d)
    roll2 = _json.loads((d.parent / "_rollup.json").read_text())
    assert roll2["totals"]["bill"] == pytest.approx(sm["bill"])
    assert roll2["totals"]["n_sessions"] == 1


def _events(d):
    return [e for t in (json.loads(x) for x in (d / "derived.jsonl").read_text().splitlines())
            for e in t["events"]]


def test_ctx_breakdown_sums_back_to_the_category_cell(tmp_path):
    d = _session(tmp_path)
    events = [e for e in _events(d) if e["zone"] == "input"]
    g, lbl = "Messages", "system tool use+output"
    rows = drill.ctx_breakdown(events, g, lbl)
    assert [r["label"] for r in rows] == ["Bash"]
    cell = sum(e.get("tokens", 0) for e in events if drill._category(e) == (g, lbl))
    assert sum(r["tokens"] for r in rows) == cell and cell > 0


def test_ctx_calls_carry_refs_that_fetch_the_real_text(tmp_path):
    d = _session(tmp_path)
    events = [e for e in _events(d) if e["zone"] == "input"]
    calls = drill.ctx_calls(events, "Messages", "system tool use+output", "Bash")
    texts = [drill.fetch_content(d, c["ref"]) for c in calls]
    assert any("alpha.py beta.py" in t for t in texts)


def test_ctx_servers_clusters_mcp_then_tools(tmp_path):
    events = [
        {"zone": "input", "section": "messages", "bucket": "tool_use",
         "tool": "mcp__notion__search", "skill": None, "role": None, "tokens": 30, "ref": None},
        {"zone": "input", "section": "messages", "bucket": "tool_result",
         "tool": "mcp__notion__fetch", "skill": None, "role": None, "tokens": 70, "ref": None},
    ]
    servers = drill.ctx_servers(events, "Messages", "MCP tool use+output")
    assert [s["label"] for s in servers] == ["notion"] and servers[0]["tokens"] == 100
    tools = drill.ctx_breakdown(events, "Messages", "MCP tool use+output", server="notion")
    assert {t["label"] for t in tools} == {"mcp__notion__search", "mcp__notion__fetch"}


def test_fetch_content_reads_codex_frame_stream(tmp_path):
    d = tmp_path / "codex" / "sess"
    d.mkdir(parents=True)
    frames = [
        {"frame": {"type": "response.create", "model": "gpt-5.5", "instructions": "sys", "tools": [],
                   "input": [{"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "hello codex"}]}]}},
        {"frame": {"type": "response.output_item.done",
                   "item": {"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "hi back"}]}}},
        {"frame": {"type": "response.completed",
                   "response": {"usage": {"input_tokens": 5, "output_tokens": 2}}}},
    ]
    (d / "raw.jsonl").write_text("\n".join(json.dumps(f) for f in frames) + "\n")
    materialize_session(d)
    evs = [e for t in (json.loads(x) for x in (d / "derived.jsonl").read_text().splitlines())
           for e in t["events"]]
    out_ev = next(e for e in evs if e["zone"] == "output" and e["bucket"] == "text")
    assert drill.fetch_content(d, out_ev["ref"]) == "hi back"
    user_ev = next(e for e in evs if e["section"] == "messages" and e["bucket"] == "text")
    assert "hello codex" in drill.fetch_content(d, user_ev["ref"])
