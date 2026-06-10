from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

_CAT = {
    "usd": 0.0, "cached_usd": 0.0, "rewrote_usd": 0.0, "fresh_usd": 0.0, "output_usd": 0.0,
}


def _summary(**by_cat):
    cats = {}
    for k, usd in by_cat.items():
        g, lbl = k.split("__", 1)
        cats[f"{g}|{lbl}"] = {**_CAT, "usd": usd, "fresh_usd": usd}
    return {"bill": sum(by_cat.values()), "columns": {"cached": 0, "rewrote": 0, "fresh": 0},
            "by_category": cats}


def test_app_and_screens_construct():
    from cost_xray.tui_app import DetailScreen, HomeScreen, XrayApp
    assert XrayApp().TITLE == "cost-xray"
    assert HomeScreen() is not None
    d = DetailScreen(["/tmp/nope"], "claude", "test", show_context=True)
    assert d.agent == "claude" and d.dirs == ["/tmp/nope"]


def test_q_quits_from_home_and_detail(monkeypatch, tmp_path):
    from cost_xray import tui
    from cost_xray.tui_app import DetailScreen, HomeScreen, XrayApp

    root = tmp_path / "sessions"
    (root / "claude").mkdir(parents=True)
    roll = {"sessions": {"aaaaaaaa1111": {"project": "/home/u/web", "name": "one", "bill": 1.0,
                                          "nt": 10, "cached": 80, "ci": 100, "tokens": 100,
                                          "mtime": 2.0}},
            "projects": {}, "totals": {}}
    monkeypatch.setattr(tui, "ROOT", root)
    monkeypatch.setattr(tui, "_rollup", lambda ad: roll if ad.name == "claude" else {"sessions": {}})
    monkeypatch.setattr(tui, "_ensure_fresh", lambda d: None)
    monkeypatch.setattr(tui, "_latest_derived", lambda d: {})
    monkeypatch.setattr(tui, "_summary", lambda d: {})

    async def go():
        app = XrayApp()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, HomeScreen)
            await app.push_screen(DetailScreen([root / "claude" / "aaaaaaaa1111"], "claude",
                                               "one", show_context=False))
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen)
            await pilot.press("q")
            await pilot.pause()
            assert app._exit is True

        app = XrayApp()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, HomeScreen)
            await pilot.press("q")
            await pilot.pause()
            assert app._exit is True

    asyncio.run(go())


def test_home_groups_agent_project_session_and_hides_noise(monkeypatch, tmp_path):
    from cost_xray import tui
    from cost_xray.tui_app import DetailScreen, HomeScreen, XrayApp

    root = tmp_path / "sessions"
    (root / "claude").mkdir(parents=True)
    (root / "unknown").mkdir(parents=True)
    rolls = {
        str(root / "claude"): {
            "sessions": {
                "aaaaaaaa1111": {"project": "/home/u/web", "name": "one", "bill": 1.0, "nt": 10,
                                 "cached": 80, "ci": 100, "tokens": 100, "mtime": 2.0},
                "bbbbbbbb2222": {"project": "/home/u/web", "name": "two", "bill": 3.0, "nt": 20,
                                 "cached": 60, "ci": 100, "tokens": 100, "mtime": 3.0},
                "cccccccc3333": {"project": "/home/u/web", "name": "quota", "bill": 0.0001, "nt": 1,
                                 "cached": 0, "ci": 1, "tokens": 1, "mtime": 1.0},
            },
            "projects": {"/home/u/web": {"bill": 4.0, "tokens": 200, "cached": 140, "ci": 200,
                                         "nt": 30, "n_sessions": 2}},
            "totals": {"bill": 4.0, "tokens": 200, "cached": 140, "ci": 200, "nt": 30, "n_sessions": 2},
        },
        str(root / "unknown"): {"sessions": {
            "dddddddd4444": {"bill": 0.0001, "nt": 1, "tokens": 1, "mtime": 9.0}}},
    }
    monkeypatch.setattr(tui, "ROOT", root)
    monkeypatch.setattr(tui, "_rollup", lambda ad: rolls.get(str(ad), {"sessions": {}}))
    monkeypatch.setattr(tui, "_ensure_fresh", lambda d: None)
    monkeypatch.setattr(tui, "_latest_derived", lambda d: {})

    async def go():
        app = XrayApp()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            home = app.screen
            assert isinstance(home, HomeScreen)

            assert "unknown" not in home._agent_nodes
            assert set(home._sess_nodes) == {"aaaaaaaa1111", "bbbbbbbb2222"}

            pnode = home._proj_nodes[("claude", "/home/u/web")]
            assert "web" in pnode.label and pnode.data == {}
            assert "$4.00" in pnode.cols and pnode.expandable
            assert len(home._proj_sids[("claude", "/home/u/web")]) == 2

            anode = home._agent_nodes["claude"]
            assert "$4.00" in anode.cols and anode.data == {} and anode.expanded

            ncol = len(home._table.columns) - 1
            for n in (anode, pnode, *pnode.children):
                assert len(n.cols) == ncol

            order = [n.data["sid"] for n in pnode.children]
            assert order == ["bbbbbbbb2222", "aaaaaaaa1111"]

            depth = len(app.screen_stack)
            home.on_drill_table_picked(type("M", (), {"node": pnode})())
            await pilot.pause()
            assert len(app.screen_stack) == depth

            snode = home._sess_nodes["aaaaaaaa1111"]
            home.on_drill_table_picked(type("M", (), {"node": snode})())
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen)
            assert app.screen.dirs == [root / "claude" / "aaaaaaaa1111"]

    asyncio.run(go())


def test_home_reorders_live_by_recent_activity(monkeypatch, tmp_path):
    from cost_xray import tui
    from cost_xray.tui_app import HomeScreen, XrayApp

    root = tmp_path / "sessions"
    (root / "claude").mkdir(parents=True)
    sessions = {
        "aaaaaaaa1111": {"project": "/home/u/web", "name": "alpha", "bill": 1.0, "nt": 10,
                         "cached": 80, "ci": 100, "tokens": 100, "mtime": 2.0},
        "bbbbbbbb2222": {"project": "/home/u/web", "name": "bravo", "bill": 3.0, "nt": 20,
                         "cached": 60, "ci": 100, "tokens": 100, "mtime": 3.0},
    }
    roll = {"sessions": sessions, "projects": {}, "totals": {}}
    monkeypatch.setattr(tui, "ROOT", root)
    monkeypatch.setattr(tui, "_rollup", lambda ad: roll if ad.name == "claude" else {"sessions": {}})
    monkeypatch.setattr(tui, "_ensure_fresh", lambda d: None)
    monkeypatch.setattr(tui, "_latest_derived", lambda d: {})

    pkey = ("claude", "/home/u/web")

    def order(home):
        return [n.data["sid"] for n in home._proj_nodes[pkey].children]

    async def go():
        app = XrayApp()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            home = app.screen
            assert isinstance(home, HomeScreen)
            assert order(home) == ["bbbbbbbb2222", "aaaaaaaa1111"]

            home._proj_nodes[pkey].expanded = True
            home._table.cursor = next(i for i, (n, _d) in enumerate(home._table._visible())
                                      if n is home._sess_nodes["aaaaaaaa1111"])

            sessions["aaaaaaaa1111"]["mtime"] = 9.0
            sessions["cccccccc3333"] = {"project": "/home/u/web", "name": "charlie", "bill": 2.0,
                                        "nt": 5, "cached": 10, "ci": 100, "tokens": 50, "mtime": 5.0}
            home._load()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert order(home) == ["aaaaaaaa1111", "cccccccc3333", "bbbbbbbb2222"]
            assert home._proj_nodes[pkey].expanded
            cur = home._table._visible()[home._table.cursor][0]
            assert cur is home._sess_nodes["aaaaaaaa1111"]

    asyncio.run(go())


def test_home_numeric_columns_align_across_depths(monkeypatch, tmp_path):
    from rich.console import Console

    from cost_xray import tui
    from cost_xray.tui_app import XrayApp

    root = tmp_path / "sessions"
    (root / "claude").mkdir(parents=True)
    rolls = {
        str(root / "claude"): {
            "sessions": {
                "aaaaaaaa1111": {"project": "/home/u/web", "name": "s", "bill": 1.0, "nt": 10,
                                 "cached": 80, "ci": 100, "tokens": 100, "mtime": 2.0},
                "bbbbbbbb2222": {"project": "/home/u/web",
                                 "name": "a much longer session label that overflows the column",
                                 "bill": 3.0, "nt": 20, "cached": 60, "ci": 100, "tokens": 100,
                                 "mtime": 3.0},
            },
            "projects": {"/home/u/web": {"bill": 4.0, "tokens": 200, "cached": 140, "ci": 200,
                                         "nt": 30, "n_sessions": 2}},
            "totals": {"bill": 4.0, "tokens": 200, "cached": 140, "ci": 200, "nt": 30, "n_sessions": 2},
        },
    }
    monkeypatch.setattr(tui, "ROOT", root)
    monkeypatch.setattr(tui, "_rollup", lambda ad: rolls.get(str(ad), {"sessions": {}}))
    monkeypatch.setattr(tui, "_ensure_fresh", lambda d: None)

    async def go():
        app = XrayApp()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            home = app.screen
            home._proj_nodes[("claude", "/home/u/web")].expanded = True
            home._table.refresh()
            await pilot.pause()
            import io
            cons = Console(record=True, width=100, file=io.StringIO())
            cons.print(home._table.render())
            text = cons.export_text()
            offsets = {ln.index("$") for ln in text.splitlines() if "$" in ln}
            assert len(offsets) == 1

    asyncio.run(go())


def test_cost_table_is_drillable_and_aligned(monkeypatch, tmp_path):
    from rich.console import Console

    from cost_xray import tui
    from cost_xray.tui_app import DetailScreen, XrayApp

    monkeypatch.setattr(tui, "_sessions", lambda: [])
    monkeypatch.setattr(tui, "_summary",
                        lambda d: _summary(**{"Static__System prompt": 0.1,
                                              "Static__MCP tools": 0.5,
                                              "Messages__assistant text": 0.3}))

    async def go():
        app = XrayApp()
        async with app.run_test():
            screen = DetailScreen([tmp_path], "claude", "detail", show_context=False)
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            assert {n.label for n in screen._cost_roots} == {"Static", "Messages"}
            cat = screen._cost_nodes[("Static", "MCP tools")]
            assert cat.label == "MCP tools" and cat.data["label"] == "MCP tools"
            assert screen._cost.footer[0].plain == "bill"
            cons = Console(width=100)
            with cons.capture() as cap:
                cons.print(screen._cost.render())
            text = cap.get()
            for h in ("source", "total$", "read$", "write$", "new$", "%bill", "bill", "MCP tools"):
                assert h in text

    asyncio.run(go())


def test_detail_renders_cost_above_context(monkeypatch, tmp_path):
    from cost_xray import tui
    from cost_xray.tui_app import DetailScreen, XrayApp

    monkeypatch.setattr(tui, "_sessions", lambda: [])
    monkeypatch.setattr(tui, "_summary", lambda d: _summary(**{"Static__System prompt": 0.1}))
    monkeypatch.setattr(tui, "_latest_derived", lambda d: {"window": 1000, "events": []})

    async def go():
        app = XrayApp()
        async with app.run_test():
            screen = DetailScreen([tmp_path], "claude", "detail", show_context=True)
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            ids = [w.id for w in screen.query("DrillTable")]
            assert ids == ["cost", "context"]

    asyncio.run(go())


def test_context_table_drills_to_mcp_server(monkeypatch, tmp_path):
    from cost_xray import tui
    from cost_xray.tui_app import DetailScreen, XrayApp

    monkeypatch.setattr(tui, "_sessions", lambda: [])
    monkeypatch.setattr(tui, "_summary", lambda d: _summary(**{"Static__System prompt": 0.1}))
    line = {"window": 1000, "events": [
        {"zone": "input", "section": "static", "bucket": "system",
         "tool": None, "skill": None, "role": None, "tokens": 100, "ref": None},
        {"zone": "input", "section": "messages", "bucket": "tool_use",
         "tool": "mcp__notion__search", "skill": None, "role": None, "tokens": 200, "ref": None},
    ]}
    monkeypatch.setattr(tui, "_latest_derived", lambda d: line)

    async def go():
        app = XrayApp()
        async with app.run_test():
            screen = DetailScreen([tmp_path], "claude", "detail", show_context=True)
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            assert {n.label for n in screen._ctx_roots} == {"Static", "Messages"}
            mcp = screen._ctx_nodes[("Messages", "MCP tool use+output")]
            assert mcp.label == "MCP tool use+output"
            servers = mcp.loader()
            assert [s.label for s in servers] == ["notion"]
            tools = servers[0].loader()
            assert [t.label for t in tools] == ["mcp__notion__search"]

    asyncio.run(go())


def test_skill_loads_render_end_to_end(monkeypatch, tmp_path):
    import json as _json

    from rich.console import Console

    from cost_xray import tui
    from cost_xray.materialize import materialize_session
    from cost_xray.tui_app import DetailScreen, XrayApp

    d = tmp_path / "claude" / "sess"
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
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_s", "name": "Skill",
                     "input": {"skill": "ascii-banner", "args": "HELLO"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_s",
                     "content": "Launching skill: ascii-banner"}]},
                {"role": "user", "content": [{"type": "text", "text":
                    "Base directory for this skill: /x/skills/ascii-banner\n# ASCII Banner\nrender a word big"}]},
            ],
        },
        "response": {"streaming": False, "body": {"content": [{"type": "text", "text": "ok"}]}},
        "usage": {"input_tokens": 50, "cache_read_input_tokens": 300,
                  "cache_creation": {"ephemeral_1h_input_tokens": 0}, "output_tokens": 10},
        "status": 200,
    }
    (d / "raw.jsonl").write_text(_json.dumps(rec) + "\n")
    materialize_session(d)
    monkeypatch.setattr(tui, "_sessions", lambda: [])
    monkeypatch.setattr(tui, "_ensure_fresh", lambda d: None)

    async def go():
        app = XrayApp()
        async with app.run_test():
            screen = DetailScreen([d], "claude", "detail", show_context=True)
            await app.push_screen(screen)
            await app.workers.wait_for_complete()

            loads = screen._cost_nodes[("Messages", "Skill loads")]
            assert [n.label for n in loads.loader()] == ["ascii-banner"]
            skills = {n.label for n in screen._cost_nodes[("Static", "Skills")].loader()}
            assert {"ascii-banner", "json-sort-keys"} <= skills
            assert ("Messages", "system tool use+output") in screen._cost_nodes

            cons = Console(width=100)
            with cons.capture() as cap:
                cons.print(screen._cost.render())
            assert "Skill loads" in cap.get()

            cload = screen._ctx_nodes[("Messages", "Skill loads")]
            assert [n.label for n in cload.loader()] == ["ascii-banner"]

    asyncio.run(go())


def test_drilltable_windows_and_follows_cursor():
    from textual.app import App

    from cost_xray.tui_app import DrillTable, Node

    class _App(App):
        def compose(self):
            t = DrillTable("t", [("src", "left", 20), ("v", "right", 6)], "blue", id="t")
            t.set_roots([Node(f"row{i}", [str(i)]) for i in range(40)])
            self.t = t
            yield t

    async def go():
        app = _App()
        async with app.run_test(size=(60, 12)) as pilot:
            app.t.focus()
            await pilot.pause()
            assert app.t._page < 40
            for _ in range(30):
                await pilot.press("down")
            assert app.t.cursor == 30
            assert app.t._offset > 0
            assert app.t._offset <= app.t.cursor <= app.t._offset + app.t._page - 1
            await pilot.press("home")
            assert app.t.cursor == 0 and app.t._offset == 0

    asyncio.run(go())


def _click(table, y):
    table.on_click(type("Click", (), {"y": y})())


def test_drilltable_click_drills_like_keyboard():
    from textual.app import App

    from cost_xray.tui_app import DrillTable, Node

    picked = []

    class _App(App):
        def compose(self):
            t = DrillTable("t", [("src", "left", 20), ("v", "right", 6)], "blue", id="t")
            t.set_roots([Node(f"row{i}", [str(i)], loader=lambda: [Node("leaf", ["x"])])
                         for i in range(5)])
            self.t = t
            yield t

        def on_drill_table_picked(self, m):
            picked.append(m.node)

    async def go():
        app = _App()
        async with app.run_test(size=(60, 20)) as pilot:
            app.t.focus()
            await pilot.pause()
            _click(app.t, 2)
            await pilot.pause()
            assert app.t.cursor == 0 and app.t.roots[0].expanded is True
            _click(app.t, 2)
            await pilot.pause()
            assert app.t.cursor == 0 and app.t.roots[0].expanded is False
            _click(app.t, 3)
            await pilot.pause()
            assert app.t.cursor == 1 and app.t.roots[1].expanded is True
            _click(app.t, 4)
            await pilot.pause()
            assert picked and picked[-1].label == "leaf"
            before = app.t.cursor
            _click(app.t, 1)
            await pilot.pause()
            assert app.t.cursor == before

    asyncio.run(go())


def test_drilltable_wheel_scrolls_window():
    from textual.app import App

    from cost_xray.tui_app import DrillTable, Node

    class _App(App):
        def compose(self):
            t = DrillTable("t", [("src", "left", 20), ("v", "right", 6)], "blue", id="t")
            t.set_roots([Node(f"row{i}", [str(i)]) for i in range(40)])
            self.t = t
            yield t

    async def go():
        app = _App()
        async with app.run_test(size=(60, 12)) as pilot:
            app.t.focus()
            await pilot.pause()
            for _ in range(5):
                app.t.on_mouse_scroll_down(type("S", (), {"stop": lambda *a: None})())
            await pilot.pause()
            assert app.t.cursor > 0 and app.t._offset > 0
            top = app.t.cursor
            for _ in range(5):
                app.t.on_mouse_scroll_up(type("S", (), {"stop": lambda *a: None})())
            await pilot.pause()
            assert app.t.cursor < top and app.t.cursor == 0

    asyncio.run(go())


def test_home_marks_capture_broken_agent_in_red(monkeypatch, tmp_path):
    from cost_xray import tui
    from cost_xray.tui_app import HomeScreen, XrayApp

    root = tmp_path / "sessions"
    (root / "claude").mkdir(parents=True)
    roll = {
        "sessions": {
            "aaaaaaaa1111": {"project": "/home/u/web", "name": "one", "bill": 1.0, "nt": 10,
                             "cached": 80, "ci": 100, "tokens": 100, "mtime": 2.0},
        },
        "projects": {"/home/u/web": {"bill": 1.0, "tokens": 100, "cached": 80, "ci": 100,
                                     "nt": 10, "n_sessions": 1}},
        "totals": {"bill": 1.0, "tokens": 100, "cached": 80, "ci": 100, "nt": 10, "n_sessions": 1},
        "broken": ["eeeeeeee5555", "ffffffff6666"],
    }
    monkeypatch.setattr(tui, "ROOT", root)
    monkeypatch.setattr(tui, "_rollup", lambda ad: roll if ad.name == "claude" else {"sessions": {}})
    monkeypatch.setattr(tui, "_ensure_fresh", lambda d: None)
    monkeypatch.setattr(tui, "_latest_derived", lambda d: {})

    async def go():
        app = XrayApp()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            home = app.screen
            assert isinstance(home, HomeScreen)
            anode = home._agent_nodes["claude"]
            assert "2 capture-broken" in anode.label
            assert anode.style == "bold red"
            await pilot.pause()

    asyncio.run(go())
