from __future__ import annotations

from cost_xray import detectors
from cost_xray import events as ev
from cost_xray.adapters import adapter_for, anthropic, openai
from cost_xray.analyze import ntok

EVENT_FIELDS = {"zone", "section", "bucket", "tool", "skill", "role",
                "tokens", "ref", "id", "type", "hash"}


def _record():
    return {
        "path": "/v1/messages",
        "model": "claude-opus-4-8",
        "status": 200,
        "request": {
            "model": "claude-opus-4-8",
            "system": "You are helpful.",
            "tools": [
                {"name": "Bash", "description": "run a shell command"},
                {"name": "Skill", "description": "execute a skill"},
                {"name": "mcp__github__create_issue", "description": "make an issue"},
            ],
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "hmm let me think"},
                    {"type": "text", "text": "let me run a command"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"cmd": "ls"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file.txt"}]},
            ],
        },
        "response": {"streaming": False, "body": {"content": [{"type": "text", "text": "done"}]}},
        "usage": {"input_tokens": 50, "cache_read_input_tokens": 1000,
                  "cache_creation": {"ephemeral_1h_input_tokens": 200}, "output_tokens": 10},
    }


def test_event_has_exact_canonical_shape():
    for e in anthropic.to_events(_record(), turn=1):
        assert set(e) == EVENT_FIELDS
        assert e["zone"] in ("input", "output")
        assert e["section"] in ("static", "messages", None)


def test_positional_path_assignment():
    paths = [(e["zone"], e["section"], e["bucket"]) for e in anthropic.to_events(_record())]
    assert ("input", "static", "system") in paths
    assert paths.count(("input", "static", "schema")) == 3
    assert ("input", "messages", "text") in paths
    assert ("input", "messages", "thinking") in paths
    assert ("input", "messages", "tool_use") in paths
    assert ("input", "messages", "tool_result") in paths
    assert ("output", None, "text") in paths


def test_tool_result_labelled_by_producing_tool():
    tr = next(e for e in anthropic.to_events(_record()) if e["bucket"] == "tool_result")
    assert tr["tool"] == "Bash"
    assert tr["role"] == "user"
    assert tr["id"] == "toolu_1"


def test_mcp_server_is_derived_from_prefix():
    gh = next(e for e in anthropic.to_events(_record()) if e["tool"] == "mcp__github__create_issue")
    assert ev.mcp_server(gh["tool"]) == "github"
    assert ev.mcp_server("Bash") is None


def test_output_tool_io_merged_and_split_mcp_vs_system():
    def mk(tool):
        return ev.make_event(zone="output", section=None, bucket="tool_use",
                             wire_type="tool_use", tool=tool, ref={})
    assert ev.category(mk("mcp__notion__search")) == ("Output", "MCP tool use+output")
    assert ev.category(mk("Read")) == ("Output", "system tool use+output")
    tr = ev.make_event(zone="output", section=None, bucket="tool_result", wire_type="tool_result",
                       tool="mcp__notion__search", ref={})
    assert ev.category(tr) == ("Output", "MCP tool use+output")
    assert ev.category(ev.make_event(zone="output", section=None, bucket="text",
                                     wire_type="text", ref={})) == ("Output", "text")


def test_unknown_type_self_appears_and_is_flagged():
    rec = {"request": {"model": "claude-opus-4-8", "messages": [
        {"role": "assistant", "content": [{"type": "image", "source": {"x": 1}}]}]}}
    evs = anthropic.to_events(rec)
    img = next(e for e in evs if e["type"] == "image")
    assert img["bucket"] == "image"
    assert "image" in ev.unknown_types(evs)


def test_openai_unknown_content_type_self_appears_and_is_flagged():
    rec = _codex_turn()
    rec["input"] = [{"type": "message", "role": "user",
                     "content": [{"type": "input_image", "image_url": "x"}]}]
    rec["output"] = []
    evs = openai.to_events(rec)
    img = next(e for e in evs if e["type"] == "input_image")
    assert img["bucket"] == "input_image"
    assert "input_image" in ev.unknown_types(evs)


def test_skill_ads_detector_is_structured_and_intermittent():
    assert detectors.skill_ads("plain prompt, no skills here") == []
    text = ("You are helpful.\n\nAvailable skills:\n"
            "- cost-xray: X-ray the current session context window\n"
            "- pr-helper: open and babysit a pull request\n\nOther instructions follow.")
    ads = detectors.skill_ads(text)
    assert [a["name"] for a in ads] == ["cost-xray", "pr-helper"]
    assert all(a["tokens"] > 0 for a in ads)


def test_skill_ad_carved_into_static_schema_when_present():
    rec = _record()
    rec["request"]["system"] = ("You are helpful.\n\nAvailable skills:\n"
                                "- cost-xray: X-ray the current context\n")
    evs = anthropic.to_events(rec)
    skill_evs = [e for e in evs if e["bucket"] == "schema" and e["tool"] == "Skill" and e["skill"]]
    assert any(e["skill"] == "cost-xray" for e in skill_evs)
    assert any(e["bucket"] == "system" for e in evs)


def test_skill_ads_header_matches_real_claude_catalog():
    text = ("The following skills are available for use with the Skill tool:\n\n"
            "- ascii-banner: render a word as a big ASCII banner\n"
            "- json-sort-keys: sort json keys recursively\n")
    assert [a["name"] for a in detectors.skill_ads(text)] == ["ascii-banner", "json-sort-keys"]


def test_skill_ads_rejects_prose_and_non_bulleted_continuations():
    catalog = ("The following skills are available for use with the Skill tool:\n\n"
               "- claude-api: Reference for the Claude API and SDK.\n"
               "TRIGGER — read BEFORE opening the file whenever the prompt names Claude.\n"
               "SKIP only when another provider is named.\n"
               "- run: Launch and drive the app.\n")
    assert [a["name"] for a in detectors.skill_ads(catalog)] == ["claude-api", "run"]
    prose = ("Only use skills listed in the user-invocable skills section.\n"
             "- Default: NO /schedule offer, most tasks just end.\n")
    assert detectors.skill_ads(prose) == []


def test_skill_ads_detected_in_system_role_message_not_just_system_field():
    rec = _record()
    rec["request"]["messages"] = [
        {"role": "system", "content":
            "# Instructions\n\nThe following skills are available for use with the Skill tool:\n"
            "- ascii-banner: render a word as a big ASCII banner\n"},
        {"role": "user", "content": "hi"},
    ]
    evs = anthropic.to_events(rec)
    skill_schema = [e for e in evs if e["bucket"] == "schema" and e["tool"] == "Skill" and e["skill"]]
    assert any(e["skill"] == "ascii-banner" for e in skill_schema)
    assert all(ev.category(e) == ("Static", "Skills") for e in skill_schema)


def test_skill_load_detector_extracts_skill_and_ignores_quotes():
    body = ("Base directory for this skill: /home/u/.claude/skills/ascii-banner\n\n"
            "# ASCII Banner\n\nrender stuff")
    assert detectors.skill_load(body) == "ascii-banner"
    assert detectors.skill_load("plain text, nothing here") is None
    assert detectors.skill_load("see the Base directory for this skill: /x/foo line") is None


def test_skill_load_message_is_its_own_per_skill_category():
    rec = _record()
    rec["request"]["messages"] = [
        {"role": "user", "content": [
            {"type": "text", "text":
                "Base directory for this skill: /home/u/.claude/skills/ascii-banner\n\n# ASCII Banner\n\nbody"}]},
    ]
    evs = anthropic.to_events(rec)
    load = next(e for e in evs if e["tool"] == "Skill" and e["skill"] == "ascii-banner"
                and e["bucket"] == "text")
    assert ev.category(load) == ("Messages", "Skill loads")


def test_skill_tool_call_is_not_split_per_skill():
    rec = _record()
    rec["request"]["messages"] = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_s", "name": "Skill",
             "input": {"skill": "ascii-banner", "args": "HELLO"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_s",
             "content": "Launching skill: ascii-banner"}]},
    ]
    evs = anthropic.to_events(rec)
    call = next(e for e in evs if e["bucket"] == "tool_use" and e["tool"] == "Skill")
    assert call["skill"] is None
    assert ev.category(call) == ("Messages", "system tool use+output")
    res = next(e for e in evs if e["bucket"] == "tool_result" and e["tool"] == "Skill")
    assert res["skill"] is None


def test_skill_loads_drill_per_skill_and_sum_to_category():
    from cost_xray.classify import reconcile_turn
    rec = _record()
    rec["request"]["messages"] = [
        {"role": "user", "content": [{"type": "text", "text":
            "Base directory for this skill: /x/skills/ascii-banner\n# A\nbody one two three"}]},
        {"role": "user", "content": [{"type": "text", "text":
            "Base directory for this skill: /x/skills/json-sort-keys\n# B\nbody four five six"}]},
    ]
    evs = anthropic.to_events(rec)
    r = reconcile_turn(evs, {"fresh": 100, "cached": 0, "rewrote": 0, "output": 0}, "claude-opus-4-8")
    keys = [k for k in r["by_cat_tool"] if k[:2] == ("Messages", "Skill loads")]
    assert {k[2] for k in keys} >= {"ascii-banner", "json-sort-keys"}
    drilled = sum(r["by_cat_tool"][k]["tokens"] for k in keys)
    assert abs(r["by_category"][("Messages", "Skill loads")]["tokens"] - drilled) < 1e-6


def test_streaming_response_is_reconstructed():
    rec = _record()
    rec["response"] = {"streaming": True, "events": [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "ponder"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hi there"}},
    ]}
    out = [e for e in anthropic.to_events(rec) if e["zone"] == "output"]
    assert {e["bucket"] for e in out} == {"thinking", "text"}


def test_dispatch_by_agent_and_path():
    assert adapter_for(agent="codex") is openai
    assert adapter_for(path="/v1/messages") is anthropic
    assert adapter_for(path="/responses") is openai


def _codex_turn():
    return {
        "model": "gpt-5.5",
        "path": "/backend-api/codex/responses",
        "instructions": "You are Codex.",
        "tools": [
            {"type": "function", "name": "exec_command", "description": "run a command"},
            {"type": "namespace", "name": "mcp__codex_apps__github"},
            {"type": "web_search", "name": None},
        ],
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "name": "exec_command", "call_id": "call_1", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "done"},
        ],
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello"}]}],
        "usage": {"input_tokens": 24243, "input_tokens_details": {"cached_tokens": 21888},
                  "output_tokens": 11},
    }


def test_codex_turn_decomposes_to_canonical_events():
    evs = openai.to_events(_codex_turn(), 0)
    paths = [(e["zone"], e["section"], e["bucket"]) for e in evs]
    assert ("input", "static", "system") in paths
    assert paths.count(("input", "static", "schema")) == 3
    assert ("input", "messages", "tool_use") in paths
    assert ("input", "messages", "tool_result") in paths
    assert ("output", None, "text") in paths
    assert any(e["tool"] == "web_search" for e in evs)
    gh = next(e for e in evs if e["tool"] == "mcp__codex_apps__github")
    assert ev.mcp_server(gh["tool"]) == "codex_apps"
    tr = next(e for e in evs if e["bucket"] == "tool_result")
    assert tr["tool"] == "exec_command"


def test_codex_function_call_counts_arguments_not_json_wrapper():
    evs = openai.to_events(_codex_turn(), 0)
    call = next(e for e in evs if e["type"] == "function_call")
    assert call["tokens"] == ntok("exec_command {}")
    assert call["tokens"] < ntok({"type": "function_call", "name": "exec_command",
                                  "call_id": "call_1", "arguments": "{}"})


def test_codex_usage_and_window():
    t = _codex_turn()
    assert openai.usage(t) == {"fresh": 2355, "cached": 21888, "rewrote": 0,
                               "output": 11, "write_1h": False, "output_reasoning": 0}
    assert openai.window(t) == 1_000_000


def test_iter_turns_reassembles_codex_frame_stream():
    frames = [
        {"ts": 1, "path": "/backend-api/codex/responses", "type": "response.create",
         "frame": {"type": "response.create", "model": "gpt-5.5",
                   "instructions": "sys", "tools": [], "input": []}},
        {"type": "response.output_item.done",
         "frame": {"type": "response.output_item.done",
                   "item": {"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "hi"}]}}},
        {"type": "response.completed",
         "frame": {"type": "response.completed",
                   "response": {"usage": {"input_tokens": 10, "output_tokens": 2}}}},
    ]
    turns = openai.iter_turns(frames)
    assert len(turns) == 1
    assert turns[0]["instructions"] == "sys"
    assert len(turns[0]["output"]) == 1
    assert turns[0]["usage"]["input_tokens"] == 10


def test_iter_turns_reconstructs_server_side_history():
    def umsg(t):
        return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}

    def create(inp):
        return {"frame": {"type": "response.create", "model": "gpt-5.5",
                          "instructions": "sys", "tools": [], "input": inp}}

    def done(t):
        return {"frame": {"type": "response.output_item.done",
                          "item": {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": t}]}}}

    def completed():
        return {"frame": {"type": "response.completed",
                          "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}}

    frames = [create([umsg("q1")]), done("a1"), completed(),
              create([umsg("q2")]), completed()]
    turns = openai.iter_turns(frames)
    assert len(turns) == 2
    assert len(turns[0]["input"]) == 1
    assert len(turns[1]["input"]) == 3
    assert turns[1]["new_input"] == [umsg("q2")]


def test_codex_history_does_not_carry_reasoning_output_as_input_thinking():
    def umsg(t):
        return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}

    def create(inp):
        return {"frame": {"type": "response.create", "model": "gpt-5.5",
                          "instructions": "sys", "tools": [], "input": inp}}

    def done(item):
        return {"frame": {"type": "response.output_item.done", "item": item}}

    def completed():
        return {"frame": {"type": "response.completed",
                          "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}}

    reasoning = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "ponder"}]}
    answer = {"type": "message", "role": "assistant",
              "content": [{"type": "output_text", "text": "a1"}]}
    frames = [create([umsg("q1")]), done(reasoning), done(answer), completed(),
              create([umsg("q2")]), completed()]

    turns = openai.iter_turns(frames)
    assert [item["type"] for item in turns[1]["input"]] == ["message", "message", "message"]
    evs = openai.to_events(turns[1], 1)
    assert not any(e["zone"] == "input" and e["bucket"] == "thinking" for e in evs)


def test_codex_compaction_resets_reconstructed_history():
    def umsg(t):
        return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}

    def create(inp):
        return {"frame": {"type": "response.create", "model": "gpt-5.5",
                          "instructions": "sys", "tools": [], "input": inp}}

    def done(t):
        return {"frame": {"type": "response.output_item.done",
                          "item": {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": t}]}}}

    def completed():
        return {"frame": {"type": "response.completed",
                          "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}}

    compact = {"type": "compaction", "encrypted_content": "opaque-server-state"}
    frames = [create([umsg("old q")]), done("old a"), completed(),
              create([umsg("summary prompt"), compact]), done("new a"), completed(),
              create([umsg("after compact")]), completed()]

    turns = openai.iter_turns(frames)
    assert [item["type"] for item in turns[0]["input"]] == ["message"]
    assert [item["type"] for item in turns[1]["input"]] == ["message", "compaction"]
    assert [item["type"] for item in turns[2]["input"]] == ["message", "compaction", "message", "message"]
    assert turns[2]["new_input"] == [umsg("after compact")]


def test_window_reads_the_anthropic_beta_header_not_the_model_name():
    rec = _record()
    rec["request_headers"] = {"anthropic-beta": "oauth-2025-04-20,context-1m-2025-08-07,effort"}
    assert anthropic.window(rec) == 1_000_000
    rec["request_headers"] = {"anthropic-beta": "oauth-2025-04-20"}
    assert anthropic.window(rec) == 200_000
    assert anthropic.window({"model": "claude-opus-4-8[1m]"}) == 1_000_000


def test_anthropic_usage_canonical():
    u = anthropic.usage(_record())
    assert u == {"fresh": 50, "cached": 1000, "rewrote": 200, "output": 10, "write_1h": True,
                 "output_reasoning": 0}


def test_openai_usage_canonical_subtracts_cached():
    u = openai.usage({"usage": {"prompt_tokens": 1200, "cached_tokens": 1000, "completion_tokens": 30}})
    assert u == {"fresh": 200, "cached": 1000, "rewrote": 0, "output": 30, "write_1h": False,
                 "output_reasoning": 0}


def test_openai_usage_surfaces_reasoning_tokens():
    rec = {"usage": {"input_tokens": 100, "output_tokens": 60,
                     "output_tokens_details": {"reasoning_tokens": 25}}}
    assert openai.usage(rec)["output_reasoning"] == 25
    assert openai.output_thinking(rec) == 25
    assert openai.output_thinking({"usage": {"output_tokens": 5}}) is None


def test_openai_window_from_model_name():
    assert openai.window({"model": "gpt-5-codex"}) == 1_000_000
    assert openai.window({"model": "gpt-4o"}) == 128_000


def test_window_and_usage_dispatch_by_agent():
    from cost_xray import adapters
    rec = _record()
    rec["request_headers"] = {"anthropic-beta": "context-1m-2025-08-07"}
    assert adapters.window(rec, agent="claude") == 1_000_000
    assert adapters.usage(rec, agent="claude")["cached"] == 1000


def _refkey(e):
    return tuple(sorted((k, str(v)) for k, v in e["ref"].items()))


def test_events_cover_every_wire_block_once():
    rec = _record()
    evs = anthropic.to_events(rec)
    req = rec["request"]
    n_msg = sum(1 if isinstance(m["content"], str) else len(m["content"]) for m in req["messages"])
    expected = 1 + len(req["tools"]) + n_msg + len(rec["response"]["body"]["content"])
    assert len(evs) == expected
    assert len({_refkey(e) for e in evs}) == len(evs)
    for mi, m in enumerate(req["messages"]):
        n = 1 if isinstance(m["content"], str) else len(m["content"])
        for bi in range(n):
            assert any(e["ref"].get("msg") == mi and e["ref"].get("block") == bi for e in evs)


def test_skill_carve_is_the_only_one_to_many_split():
    rec = _record()
    n0 = len(anthropic.to_events(rec))
    rec["request"]["system"] = ("You are helpful.\n\nAvailable skills:\n"
                                "- cost-xray: X-ray the current context\n"
                                "- pr-helper: open and babysit a pull request\n")
    evs = anthropic.to_events(rec)
    assert len(evs) == n0 + 2
    assert sum(e["bucket"] == "system" for e in evs) == 1
    assert len({_refkey(e) for e in evs}) == len(evs)


def test_streaming_output_event_count_matches_content_block_starts():
    rec = _record()
    rec["response"] = {"streaming": True, "events": [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "x"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hi"}},
    ]}
    out = [e for e in anthropic.to_events(rec) if e["zone"] == "output"]
    assert len(out) == 2


def test_malformed_blocks_are_skipped_not_phantomed():
    rec = {"request": {"model": "claude-opus-4-8", "messages": [
        {"role": "user", "content": [None, {"type": "text", "text": "hi"}, "rawstring"]},
        {"role": "assistant", "content": []},
    ]}}
    evs = anthropic.to_events(rec)
    assert sum(e["bucket"] == "text" for e in evs) == 1
    assert len({_refkey(e) for e in evs}) == len(evs)


def test_session_name_is_first_human_user_message():
    from cost_xray import adapters
    quota = {"request": {"messages": [{"role": "user", "content": "quota"}]}}
    convo = {"request": {"messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>ignore me</system-reminder>"},
            {"type": "text", "text": "fix the codex cache bug"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]}}
    assert adapters.session_name([quota, convo], agent="claude") == "fix the codex cache bug"
    assert adapters.session_name([quota], agent="claude") is None
    long = {"request": {"messages": [{"role": "user", "content": "x" * 80}]}}
    nm = adapters.session_name([long], agent="claude")
    assert nm.endswith("…") and len(nm) == 41
    codex = [{"frame": {"type": "response.create", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello codex"}]}]}}]
    assert adapters.session_name(codex, agent="codex") == "hello codex"


def test_codex_session_name_skips_startup_probe_and_injected_wrappers():
    from cost_xray import adapters
    probe = {"frame": {"type": "response.create", "input": []}}
    real = {"frame": {"type": "response.create", "input": [
        {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": "<permissions instructions>sandboxing</permissions instructions>"}]},
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>\n  <cwd>/home/u/proj</cwd>\n</environment_context>"}]},
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "ok"}]}]}}
    assert adapters.session_name([probe, real], agent="codex") == "ok"
    assert adapters.session_name([probe], agent="codex") is None


def test_project_name_anchored_to_env_block_not_conversation():
    from cost_xray import adapters
    env = ("# Environment\nYou have been invoked in the following environment: \n"
           " - Primary working directory: /home/u/tigerless_ai/context-xray\n - Is a git repository: true")
    rec = {"request": {"system": env, "messages": [
        {"role": "user", "content": [{"type": "text", "text":
            "Here is your environment:\n - Primary working directory: /home/u/other/proj_x"}]},
        {"role": "assistant", "content": [{"type": "text",
            "text": "Primary working directory: /home/u/wrong/place"}]}]}}
    assert adapters.project_name([rec], agent="claude") == "/home/u/tigerless_ai/context-xray"
    assert adapters.project_name([{"request": {"messages": []}}], agent="claude") is None
