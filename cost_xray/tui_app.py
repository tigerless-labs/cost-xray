"""Interactive cost-xray TUI (Textual) — mouse + keyboard.

- **Home**: agent → project → session in the **same `DrillTable` as Detail** — one Rich table, so
  the turns / hit / tokens / $ columns stay aligned however many groups are expanded (indent only
  moves the source column). Agent & project rows are **totals-only headers** — aggregate basics
  summed from the already-materialized per-session `summary.json`, not drillable. (A merged
  all-sessions Detail would have to re-read + regroup every session's `derived.jsonl` at once → large
  latency, so we don't offer it.) A session **leaf** posts `Picked` → its Detail.
- **Detail**: two codeburn-style bordered tables — **cost** (cumulative $ by the `/context`
  categories) and **context** (this turn's window occupancy). Both are **drillable**: ⏎ on a row
  → MCP server → tool → per-turn/block → the real output (fetched from `raw` on demand). Drilling
  uses real table columns, so the $ columns stay aligned at any depth (indent only goes in the
  `source` column; numeric columns are fixed-width, right-justified). `esc`/`s` → back.

Server/tool come from the pre-aggregated `summary.by_cat_tool` (fast); only the per-turn level
reads `derived`. Data layer reused (`tui` + `drill` + the materialized `summary.json`); this is
just the Textual shell. `pip install -e '.[tui]'` · `python -m cost_xray.tui_app`
"""
from __future__ import annotations

import os
from collections import defaultdict

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Footer, Header, Static

from cost_xray import drill, tui
from cost_xray import events as ev

_DRILL_LIMIT = 50                                    # cap per-occurrence leaves (avoid 10k+ rows)
_CHROME_ROWS = 2                                     # panel top border + table header above row 0
_WHEEL = 3                                           # rows the cursor moves per wheel notch


# --- drillable bordered table (one Rich Table → columns align at any depth) -----------

class Node:
    """One row in the drill tree. `cols` are pre-formatted numeric cells (1:1 with the headers);
    `loader` lazily builds children on first expand."""
    __slots__ = ("label", "cols", "data", "expandable", "loader", "children", "expanded", "bold")

    def __init__(self, label, cols, *, data=None, loader=None, bold=False,
                 children=None, expanded=False):
        self.label = label
        self.cols = cols
        self.data = data or {}
        self.loader = loader
        self.expandable = loader is not None or children is not None
        self.children = children        # None = not yet loaded (lazy) or no children (leaf)
        self.expanded = expanded
        self.bold = bold


class DrillTable(Widget):
    """Bordered table + indented expandable rows. Numeric columns are fixed-width / right-justified,
    so they stay aligned no matter how deep you drill (indent only touches the `source` column).
    Mouse + keyboard: ↑↓ / wheel move, ⏎ or a click drills the row (a parent expands, a re-click
    collapses; a leaf posts Picked so the screen can fetch its real content)."""

    can_focus = True
    BINDINGS = [
        Binding("up", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("enter", "activate", "expand/view"),
        Binding("right", "activate", "expand", show=False),
        Binding("left", "collapse", "collapse", show=False),
        Binding("pageup", "page(-1)", "page up", show=False),
        Binding("pagedown", "page(1)", "page down", show=False),
        Binding("home", "jump(-1)", "top", show=False),
        Binding("end", "jump(1)", "bottom", show=False),
    ]

    class Picked(Message):
        def __init__(self, node):
            self.node = node
            super().__init__()

    def __init__(self, title, columns, border, **kw):
        super().__init__(**kw)
        self.title = title
        self.columns = columns          # [(header, justify, width)]; columns[0] is the source column
        self.border = border
        self.roots: list[Node] = []
        self.footer = None              # totals row (not selectable)
        self.cursor = 0
        self._offset = 0                # first visible row — follows the cursor (internal scroll)
        self._page = 1                  # body rows that fit (set in render from the widget height)

    def set_roots(self, roots):
        self.roots = roots

    def _visible(self):
        out = []

        def walk(nodes, depth):
            for n in nodes:
                out.append((n, depth))
                if n.expanded and n.children:
                    walk(n.children, depth + 1)

        walk(self.roots, 0)
        return out

    def render(self):
        vis = self._visible()
        total = len(vis)
        if total:
            self.cursor = max(0, min(total - 1, self.cursor))
        # body rows that fit = widget height − panel borders(2) − header(1) − footer(1)
        page = max(1, (self.size.height or 24) - 3 - (1 if self.footer else 0))
        self._page = page
        off = self._offset                          # scroll so the cursor stays on screen
        if self.cursor < off:
            off = self.cursor
        elif self.cursor >= off + page:
            off = self.cursor - page + 1
        off = max(0, min(off, max(0, total - page)))
        self._offset = off

        t = Table(show_header=True, header_style="grey50", box=None, padding=(0, 1), expand=False)
        for i, (head, justify, width) in enumerate(self.columns):
            t.add_column(head, justify=justify, width=width,
                         no_wrap=(i == 0), overflow="ellipsis")
        for i in range(off, min(off + page, total)):
            n, depth = vis[i]
            arrow = "▾ " if (n.expandable and n.expanded) else ("▸ " if n.expandable else "  ")
            name = Text("  " * depth + arrow + n.label, style="bold" if n.bold else "")
            style = "reverse" if (i == self.cursor and self.has_focus) else None
            t.add_row(name, *n.cols, style=style)
        if self.footer:
            t.add_row(*self.footer, style="bold")
        # scroll hint: ▲/▼ when clipped, plus the visible window / total
        lo, hi = (off + 1 if total else 0), min(off + page, total)
        up, down = ("▲" if off > 0 else " "), ("▼" if off + page < total else " ")
        sub = f"{up} {lo}-{hi}/{total} {down} · ↑↓ ⏎"
        return Panel(t, title=self.title, border_style=self.border, title_align="left",
                     subtitle=sub, subtitle_align="right")

    def _move(self, d):
        vis = self._visible()
        if vis:
            self.cursor = max(0, min(len(vis) - 1, self.cursor + d))
            self.refresh()

    def action_page(self, d: int):
        self._move(d * self._page)

    def action_jump(self, d: int):
        self.cursor = 0 if d < 0 else max(0, len(self._visible()) - 1)
        self.refresh()

    def action_cursor_up(self):
        self._move(-1)

    def action_cursor_down(self):
        self._move(1)

    def _activate(self, *, toggle=False):
        vis = self._visible()
        if not vis:
            return
        n = vis[self.cursor][0]
        if not n.expandable:
            self.post_message(self.Picked(n))
            return
        if toggle and n.expanded:
            n.expanded = False
            self.refresh()
            return
        if n.children is None:
            n.children = n.loader() or []
        n.expanded = True
        self.refresh()

    def action_activate(self):
        self._activate()

    def action_collapse(self):
        vis = self._visible()
        if vis and vis[self.cursor][0].expanded:
            vis[self.cursor][0].expanded = False
            self.refresh()

    def on_click(self, event):
        self.focus()
        row = event.y - _CHROME_ROWS                 # body starts below the border + header
        if not (0 <= row < self._page):              # a click on the chrome / footer drills nothing
            return
        i = self._offset + row
        if i < len(self._visible()):
            self.cursor = i
            self._activate(toggle=True)              # a click toggles a parent; ⏎ only expands

    def on_mouse_scroll_down(self, event):
        event.stop()
        self._move(_WHEEL)

    def on_mouse_scroll_up(self, event):
        event.stop()
        self._move(-_WHEEL)


# --- Home: agent → project → session, in the same DrillTable as Detail ---------------

_MIN_BILL = 0.005       # hide sessions costing less than this (rounds to $0.00 — e.g. quota probes)
_HOME_COLS = [("agent · project · session", "left", 40), ("turns", "right", 6),
              ("hit", "right", 5), ("tokens", "right", 8), ("cost", "right", 9)]


def _home_cols(nt, cached, ci, tokens, bill):
    """The four numeric cells shared by every Home row (agent/project/session) — same count and
    order at every depth, so they slot into the same fixed-width columns and stay aligned."""
    hit = (cached / ci) if ci else 0.0
    return [f"{int(nt)}", f"{100*hit:.0f}%", tui._h(int(tokens)), f"${bill:.2f}"]


class HomeScreen(Screen):
    """agent → **project** → session, rendered in the **same `DrillTable` as Detail** — one Rich
    table, indent only on the source column, so the numeric columns stay aligned however many groups
    are expanded. Agents expand by default, projects collapse; a session **leaf** posts `Picked` →
    Detail. `unknown`-agent and $0 sessions are hidden; agent/project rows are totals-only."""
    BINDINGS = [Binding("q", "app.quit", "quit")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield DrillTable("agent · project · session", _HOME_COLS, "green", id="home")
        yield Footer()

    def on_mount(self) -> None:
        self._table = self.query_one("#home", DrillTable)
        self._dirs = {}                                  # sid → (dir, agent)
        self._sess_nodes, self._proj_nodes, self._agent_nodes = {}, {}, {}
        self._proj_sids = defaultdict(list)              # (agent, project) → [sid], mtime order
        self._agent_sids = defaultdict(list)             # agent → [sid]
        self._names = {}
        self._expanded = {}                              # agent / pkey → open?, kept across live re-sorts
        rolls, agents = self._collect()
        self._sync(rolls, agents)
        if self._dirs:
            self._table.focus()
            self.set_interval(2.0, self._load)           # live: re-sort + bills follow the materializer

    def _collect(self):
        """Read every agent's **rollup** (one read/agent) and group + sort its sessions
        newest-active-first, projects/agents by newest activity, **`(no project)` last**. `unknown` +
        $0 sessions hidden. Pure reads (no widget/state mutation) so the poll worker runs it off the
        main thread. Returns `(rolls, agents)`."""
        rolls, agents = [], []
        if tui.ROOT.exists():
            for ad in sorted(tui.ROOT.iterdir()):
                if not ad.is_dir() or ad.name == "unknown":
                    continue
                roll = tui._rollup(ad) or {}
                rolls.append((ad.name, roll))
                groups = defaultdict(list)
                for sid, b in (roll.get("sessions") or {}).items():
                    if b.get("bill", 0.0) < _MIN_BILL:   # $0 / 0-turn → hide (rounds to $0.00)
                        continue
                    d = ad / sid
                    tui._ensure_fresh(d)                 # keep live capture flowing (stat-only kick)
                    groups[b.get("project") or "—"].append((b.get("mtime", 0.0), sid, d, b))
                if groups:
                    for g in groups.values():
                        g.sort(key=lambda r: -r[0])
                    agents.append((ad.name, max(g[0][0] for g in groups.values()), groups))
        agents.sort(key=lambda a: -a[1])                 # agent by most-recent activity
        return rolls, agents

    def _sync(self, rolls, agents) -> None:
        """(Re)build the agent→project→session tree in newest-active order — run on the main thread on
        mount and on every poll. Carries each group's expand/collapse forward and keeps the cursor on
        the same row as it moves; `_paint` then fills the live numbers."""
        for agent, an in self._agent_nodes.items():      # snapshot the user's open/closed choices
            self._expanded[agent] = an.expanded
        for pkey, pn in self._proj_nodes.items():
            self._expanded[pkey] = pn.expanded
        cursor = self._cursor_id()
        self._dirs = {}
        self._sess_nodes, self._proj_nodes, self._agent_nodes = {}, {}, {}
        self._proj_sids = defaultdict(list)
        self._agent_sids = defaultdict(list)
        blank, roots = ["", "", "", ""], []
        for agent, _mt, groups in agents:
            pnodes = []
            # real projects by newest session desc; `(no project)` ("—") always last
            for proj in sorted(groups, key=lambda p: (p == "—", -groups[p][0][0])):
                pkey = (agent, proj)
                snodes = []
                for _m, sid, d, b in groups[proj]:
                    self._dirs[sid] = (d, agent)
                    self._agent_sids[agent].append(sid)
                    self._proj_sids[pkey].append(sid)
                    if b.get("name"):
                        self._names[sid] = b["name"]
                    snode = Node(self._names.get(sid) or sid[:8], blank,
                                 data={"dir": d, "agent": agent, "sid": sid, "proj": pkey})
                    snodes.append(snode)
                    self._sess_nodes[sid] = snode
                label = "(no project)" if proj == "—" else os.path.basename(proj)
                pnode = Node(label, blank, children=snodes,
                             expanded=self._expanded.get(pkey, False))
                self._proj_nodes[pkey] = pnode
                pnodes.append(pnode)
            anode = Node(agent, blank, bold=True, children=pnodes,
                         expanded=self._expanded.get(agent, True))
            self._agent_nodes[agent] = anode
            roots.append(anode)
        self._table.set_roots(roots)
        self._restore_cursor(cursor)
        self._paint(rolls)

    def _cursor_id(self):
        """Identity `(kind, key)` of the row under the cursor, so a re-sort can re-find it."""
        vis = self._table._visible()
        if not vis or not (0 <= self._table.cursor < len(vis)):
            return None
        node = vis[self._table.cursor][0]
        for sid, n in self._sess_nodes.items():
            if n is node:
                return ("s", sid)
        for pkey, n in self._proj_nodes.items():
            if n is node:
                return ("p", pkey)
        for agent, n in self._agent_nodes.items():
            if n is node:
                return ("a", agent)
        return None

    def _restore_cursor(self, cid) -> None:
        if not cid:
            return
        kind, key = cid
        target = (self._sess_nodes.get(key) if kind == "s"
                  else self._proj_nodes.get(key) if kind == "p"
                  else self._agent_nodes.get(key))
        if target is None:
            return
        for i, (n, _depth) in enumerate(self._table._visible()):
            if n is target:
                self._table.cursor = i
                return

    @work(thread=True, exclusive=True)
    def _load(self) -> None:
        rolls, agents = self._collect()                  # off-thread reads; the tree mutate is main-thread
        self.app.call_from_thread(self._sync, rolls, agents)

    def _paint(self, rolls) -> None:
        """Repaint every node's label + numeric cells from the per-agent rollup — session leaves from
        `sessions`, project & agent groups from the **precomputed** `projects` / `totals` (no summing;
        the materializer refreshed them on the last derived update). A footer pins the grand total."""
        gt = dict.fromkeys(("nt", "cached", "ci", "tokens", "bill"), 0.0)
        for agent, roll in rolls:
            for sid, b in (roll.get("sessions") or {}).items():
                node = self._sess_nodes.get(sid)
                if node is None:
                    continue
                if b.get("name"):
                    self._names[sid] = b["name"]
                    node.label = b["name"]
                node.cols = _home_cols(b.get("nt", 0), b.get("cached", 0), b.get("ci", 0),
                                       b.get("tokens", 0), b.get("bill", 0.0))
            projs = roll.get("projects") or {}
            for (a, proj), pnode in self._proj_nodes.items():
                if a != agent:
                    continue
                pt = projs.get(proj)
                if pt:
                    base = os.path.basename(proj) if proj != "—" else "(no project)"
                    pnode.label = f"{base} · {int(pt['n_sessions'])} sess"
                    pnode.cols = _home_cols(pt.get("nt", 0), pt.get("cached", 0), pt.get("ci", 0),
                                            pt.get("tokens", 0), pt.get("bill", 0.0))
            anode, tot = self._agent_nodes.get(agent), roll.get("totals") or {}
            if anode is not None and tot:
                anode.label = f"{agent} · {int(tot.get('n_sessions', 0))} sess"
                anode.cols = _home_cols(tot.get("nt", 0), tot.get("cached", 0), tot.get("ci", 0),
                                        tot.get("tokens", 0), tot.get("bill", 0.0))
                for k in gt:
                    gt[k] += tot.get(k, 0.0)
        self._table.footer = [Text("total", style="bold"),
                              *_home_cols(gt["nt"], gt["cached"], gt["ci"], gt["tokens"], gt["bill"])]
        self._table.refresh()

    def on_drill_table_picked(self, message: DrillTable.Picked) -> None:
        data = message.node.data
        if not data or "dir" not in data:
            return                              # agent/project rows are totals-only (no merged detail)
        nm = self._names.get(data["sid"])
        title = f"{nm} · {data['sid'][:8]}" if nm else data["sid"][:8]
        self.app.push_screen(DetailScreen([data["dir"]], data["agent"], title, show_context=True))



# --- Detail: cost + context drill tables + content panel ---------------------------

_COST_COLS = [("source", "left", 22), ("total$", "right", 9), ("read$", "right", 8),
              ("write$", "right", 8), ("new$", "right", 8), ("%bill", "right", 6)]
_CTX_COLS = [("source", "left", 22), ("", "left", 16), ("tokens", "right", 8), ("%win", "right", 6)]


class DetailScreen(Screen):
    BINDINGS = [Binding("escape,s", "app.pop_screen", "back"), Binding("q", "app.quit", "quit")]

    def __init__(self, dirs, agent, title, show_context):
        super().__init__()
        self.dirs, self.agent, self.detail_title = dirs, agent, title
        self.show_context = show_context and len(dirs) == 1
        self._bill = 0.0
        self._cost = DrillTable("cost · cumulative", _COST_COLS, "magenta", id="cost")
        self._cost_nodes = {}            # group / (group,label) → Node
        self._cost_roots = []
        if self.show_context:
            self._ctx = DrillTable("context · this turn", _CTX_COLS, "blue", id="context")
            self._ctx_nodes = {}
            self._ctx_roots = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="title")
        if self.show_context:
            yield self._ctx
        else:
            yield Static(id="ctxnote")
        yield self._cost
        yield Static(id="content")
        yield Footer()

    def on_mount(self) -> None:
        self._cost.set_roots(self._cost_roots)
        if self.show_context:
            self._ctx.set_roots(self._ctx_roots)
        else:
            self.query_one("#ctxnote", Static).update(Panel(
                Text("context is single-session only (an aggregate has no one window)",
                     style="grey50"), title="context", border_style="grey37", title_align="left"))
        self._refresh()
        self.query_one("#content", Static).update(Text("", style="grey50"))
        self._cost.focus()
        self.set_interval(1.5, self._refresh)

    # ---- cost table (cumulative summary) ----
    def _agg(self):
        agg = defaultdict(lambda: dict.fromkeys(
            ("usd", "cached_usd", "rewrote_usd", "fresh_usd", "output_usd"), 0.0))
        bill = 0.0
        for d in self.dirs:
            sm = tui._summary(d) or {}
            bill += sm.get("bill", 0.0)
            for k, v in sm.get("by_category", {}).items():
                g, lbl = k.split("|", 1) if isinstance(k, str) else k
                a = agg[(g, lbl)]
                for f in a:
                    a[f] += v.get(f, 0.0)
        return agg, bill

    def _pct(self, usd):
        return f"{100*usd/self._bill:.0f}%" if self._bill else "—"

    def _split_cols(self, usd, cr, cw, nw):
        return [f"${usd:.2f}", f"${cr:.2f}", f"${cw:.2f}", f"${nw:.2f}", self._pct(usd)]

    def _row_cols(self, r):              # server/tool row — full cache $ split (from summary)
        nw = r.get("fresh_usd", 0.0) + r.get("output_usd", 0.0)
        return self._split_cols(r["usd"], r.get("cached_usd", 0.0), r.get("rewrote_usd", 0.0), nw)

    def _leaf_cols(self, usd):           # per-turn call row — total$ + %bill only (still aligned)
        return [f"${usd:.2f}", "", "", "", self._pct(usd)]

    def _cat_loader(self, g, lbl):
        def load():
            if lbl.startswith("MCP"):                         # MCP category → cluster by server first
                return [self._cost_server_node(g, lbl, r) for r in drill.cat_servers(self.dirs, g, lbl)]
            return [self._cost_tool_node(g, lbl, None, r) for r in drill.cat_breakdown(self.dirs, g, lbl)]
        return load

    def _cost_server_node(self, g, lbl, r):
        return Node(r["label"], self._row_cols(r),
                    loader=lambda: [self._cost_tool_node(g, lbl, r["label"], x)
                                    for x in drill.cat_breakdown(self.dirs, g, lbl, server=r["label"])])

    def _cost_tool_node(self, g, lbl, server, r):
        return Node(r["label"], self._row_cols(r),
                    loader=lambda: self._cost_call_nodes(g, lbl, r["label"]))

    def _cost_call_nodes(self, g, lbl, tool):
        calls = drill.cat_calls(self.dirs, g, lbl, tool)      # per-turn level reads derived
        nodes = [Node(f"turn#{c['turn']}", self._leaf_cols(c["usd"]),
                      data={"ref": c["ref"], "dir": c["dir"]}) for c in calls[:_DRILL_LIMIT]]
        if len(calls) > _DRILL_LIMIT:
            nodes.append(Node(f"… {len(calls) - _DRILL_LIMIT:,} more", ["", "", "", "", ""]))
        return nodes

    def _refresh_cost(self):
        agg, bill = self._agg()
        self._bill = bill
        groups = defaultdict(dict)
        for (g, lbl), v in agg.items():
            groups[g][lbl] = v
        gt = [0.0, 0.0, 0.0, 0.0]
        for g in ("Static", "Messages", "Output"):
            cats = groups.get(g)
            if not cats:
                continue
            gnode = self._cost_nodes.get(g)
            if gnode is None:
                gnode = Node(g, ["", "", "", "", ""], bold=True, loader=lambda: [])
                gnode.children, gnode.expanded = [], True
                self._cost_nodes[g] = gnode
                self._cost_roots.append(gnode)
            sub = [0.0, 0.0, 0.0, 0.0]
            for lbl, v in sorted(cats.items(), key=lambda x: -x[1]["usd"]):
                nw = v["fresh_usd"] + v["output_usd"]
                for i, x in enumerate((v["usd"], v["cached_usd"], v["rewrote_usd"], nw)):
                    sub[i] += x
                cols = self._split_cols(v["usd"], v["cached_usd"], v["rewrote_usd"], nw)
                cnode = self._cost_nodes.get((g, lbl))
                if cnode is None:
                    cnode = Node(lbl, cols, data={"group": g, "label": lbl},
                                 loader=self._cat_loader(g, lbl))
                    gnode.children.append(cnode)
                    self._cost_nodes[(g, lbl)] = cnode
                else:
                    cnode.cols = cols
            for i in range(4):
                gt[i] += sub[i]
            gnode.cols = self._split_cols(*sub)
        self._cost.footer = [Text("bill", style="bold"), f"${gt[0]:.2f}", f"${gt[1]:.2f}",
                             f"${gt[2]:.2f}", f"${gt[3]:.2f}", "—"]
        self._cost.refresh()

    # ---- context table (this turn's window snapshot; same server→tool→block drill) ----
    def _latest_input_events(self):
        line = tui._latest_derived(self.dirs[0]) or {}
        win = line.get("window") or 1
        return [e for e in line.get("events", []) if e.get("zone") == "input"], win

    def _tok_cols(self, tok, win):
        frac = tok / win if win else 0.0
        return [tui._bar(frac, 14, "blue"), tui._h(int(tok)), f"{100*frac:.0f}%"]

    def _ctx_cat_loader(self, g, lbl):
        def load():
            events, win = self._latest_input_events()
            if lbl.startswith("MCP"):
                return [self._ctx_server_node(g, lbl, r, win) for r in drill.ctx_servers(events, g, lbl)]
            return [self._ctx_tool_node(g, lbl, None, r, win) for r in drill.ctx_breakdown(events, g, lbl)]
        return load

    def _ctx_server_node(self, g, lbl, r, win):
        def load():
            events, w = self._latest_input_events()
            return [self._ctx_tool_node(g, lbl, r["label"], x, w)
                    for x in drill.ctx_breakdown(events, g, lbl, server=r["label"])]
        return Node(r["label"], self._tok_cols(r["tokens"], win), loader=load)

    def _ctx_tool_node(self, g, lbl, server, r, win):
        return Node(r["label"], self._tok_cols(r["tokens"], win),
                    loader=lambda: self._ctx_call_nodes(g, lbl, r["label"], win))

    def _ctx_call_nodes(self, g, lbl, tool, win):
        events, _ = self._latest_input_events()
        calls = drill.ctx_calls(events, g, lbl, tool)
        return [Node(f"block {i+1}", self._tok_cols(c["tokens"], win),
                     data={"ref": c["ref"], "dir": self.dirs[0]})
                for i, c in enumerate(calls[:_DRILL_LIMIT])]

    def _refresh_context(self):
        events, win = self._latest_input_events()
        cats = defaultdict(float)
        for e in events:
            g, lbl = ev.category(e)
            if g in ("Static", "Messages"):
                cats[(g, lbl)] += e.get("tokens", 0)
        groups, total = defaultdict(dict), 0.0
        for (g, lbl), tok in cats.items():
            groups[g][lbl] = tok
            total += tok
        for g in ("Static", "Messages"):
            gcats = groups.get(g)
            if not gcats:
                continue
            gnode = self._ctx_nodes.get(g)
            if gnode is None:
                gnode = Node(g, self._tok_cols(0, win), bold=True, loader=lambda: [])
                gnode.children, gnode.expanded = [], True
                self._ctx_nodes[g] = gnode
                self._ctx_roots.append(gnode)
            gtok = 0.0
            for lbl, tok in sorted(gcats.items(), key=lambda x: -x[1]):
                gtok += tok
                cnode = self._ctx_nodes.get((g, lbl))
                if cnode is None:
                    cnode = Node(lbl, self._tok_cols(tok, win), data={"group": g, "label": lbl},
                                 loader=self._ctx_cat_loader(g, lbl))
                    gnode.children.append(cnode)
                    self._ctx_nodes[(g, lbl)] = cnode
                else:
                    cnode.cols = self._tok_cols(tok, win)
            gnode.cols = self._tok_cols(gtok, win)
        free = max(0, win - int(total))
        self._ctx.title = f"context · this turn · window {tui._h(win)} · free {tui._h(free)}"
        self._ctx.footer = [Text("total", style="bold"), tui._bar(total / win if win else 0, 14, "cyan"),
                            tui._h(int(total)), f"{100*total/win:.0f}%" if win else "—"]
        self._ctx.refresh()

    def _refresh(self) -> None:
        self.query_one("#title", Static).update(
            Text(f"{self.detail_title} · ${self._bill:.2f} · {len(self.dirs)} session(s)", style="bold"))
        self._refresh_cost()
        if self.show_context:
            self._refresh_context()

    def on_drill_table_picked(self, message: DrillTable.Picked) -> None:
        data = message.node.data
        if data and "ref" in data:
            text = drill.fetch_content(data.get("dir", self.dirs[0]), data["ref"]) or "(empty / not resolvable)"
            self.query_one("#content", Static).update(
                Panel(Text(text[:4000]), title="output", border_style="grey37", title_align="left"))


class XrayApp(App):
    TITLE = "cost-xray"
    CSS = """
    #title { height: 1; padding: 0 1; }
    #ctxnote { height: auto; }
    #content { height: auto; max-height: 10; overflow-y: auto; }
    DrillTable { height: 1fr; min-height: 6; }
    """

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())


def main() -> None:
    XrayApp().run()


if __name__ == "__main__":
    main()
