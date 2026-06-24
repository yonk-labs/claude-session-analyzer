"""Interactive TUI for browsing Claude Code usage.

Drill-down screens, each an aggregation of the same parsed model:
  Browser  -> sessions under a root (sortable: $, tokens, time, tok/s)
  Session  -> bucketed bar table (tokens/spend/turns) + sortable turns;
              click a bucket to filter turns to that time window
  Turn     -> the commands/tool-calls in one turn, with friction flags
              (a Task/Agent row drills into the subagent it spawned)
  Subagents-> the subagents a session spawned (press 'b' in a session), each a
              mini-transcript reusing the Turn/Step screens
  Skills   -> corpus-wide per-skill regret leaderboard (press 's' in browser)

Honest labels (abe review): tok/s is END-TO-END throughput, not decode speed;
friction/regret is suspicion, not proof of harm.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from . import model, pricing


def human(n):
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.0f}k"
    return str(int(n))


def when(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if isinstance(dt, datetime) else "?"


def short_proj(p, w=30):
    return model.pretty_project(p, w)


def short_file(path, w=58):
    """Collapse $HOME to ~ and keep the tail (filename) visible."""
    p = path.replace(str(Path.home()), "~")
    return p if len(p) <= w else "…" + p[-(w - 1):]


def human_bytes(n):
    if n is None:
        return "gone"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f}M"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f}K"
    return f"{n}B"


def _bar(val, maxv, width=10):
    n = int(round(val / maxv * width)) if maxv else 0
    return "█" * n + " " * (width - n)


class Sortable:
    """Keyboard sort for table screens: press 1-9 to sort by that column (press
    the same number again to reverse). Mouse: click the header. Screens expose
    COLS, sort_i, sort_rev and a _resort() that re-renders the table."""

    def on_key(self, event):
        key = event.key
        if key in "123456789" and getattr(self, "COLS", None):
            i = int(key) - 1
            if 0 <= i < len(self.COLS):
                self.sort_rev = (self.COLS[i][2] if i != self.sort_i
                                 else not self.sort_rev)
                self.sort_i = i
                self._resort()
                event.stop()

    def _resort(self):
        self._fill()


# --------------------------------------------------------------------------- #
class ProjectsScreen(Sortable, Screen):
    """Landing screen: sessions rolled up per project. Enter a project to see its
    sessions, or 'a' for every session across all projects."""
    BINDINGS = [("q", "app.quit", "Quit"), ("a", "all_sessions", "All sessions"),
                ("s", "skills", "Skill regret"), ("t", "tools", "Tools"),
                ("m", "mcp", "MCP")]
    COLS = [
        ("project", lambda d: d["project"], False),
        ("sessions", lambda d: d["sessions"], True),
        ("$", lambda d: d["cost"], True),
        ("out", lambda d: d["out"], True),
        ("in+cache", lambda d: d["ctx_in"], True),
        ("last used", lambda d: d["tmax"] or datetime.min, True),
    ]

    def __init__(self, root):
        super().__init__()
        self.root = root
        self.summaries = []
        self.rows = []
        self.sort_i, self.sort_rev = 2, True   # $ desc

    def compose(self):
        yield Header()
        self.status = Static("Scanning transcripts…", id="status")
        yield self.status
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = "projects"
        self.table.add_columns(*[c[0] for c in self.COLS])
        self.load()

    @work(thread=True, exclusive=True)
    def load(self):
        rows = self.app.cached_corpus(self.root)
        self.app.call_from_thread(self._populate, rows)

    def reload(self):
        self.load()

    def _populate(self, summaries):
        self.summaries = summaries
        self.rows = model.project_totals(summaries)
        total = sum(s.cost for s in summaries)
        self.status.update(
            f"[b]{len(self.rows)}[/b] projects · [b]{len(summaries)}[/b] sessions · "
            f"~[b]${total:,.0f}[/b] · Enter a project · [b]a[/b]=all · "
            f"[b]s[/b]=skills [b]t[/b]=tools [b]m[/b]=MCP · "
            f"[b]1-9[/b]/click header=sort")
        self._fill()

    def _fill(self):
        self.rows.sort(key=self.COLS[self.sort_i][1], reverse=self.sort_rev)
        self.table.clear()
        for i, d in enumerate(self.rows):
            self.table.add_row(
                short_proj(d["project"], 46), str(d["sessions"]),
                f"${d['cost']:,.2f}", human(d["out"]), human(d["ctx_in"]),
                when(d["tmax"]), key=str(i))

    def on_data_table_header_selected(self, e):
        i = e.column_index
        self.sort_rev = self.COLS[i][2] if i != self.sort_i else not self.sort_rev
        self.sort_i = i
        self._fill()

    def on_data_table_row_selected(self, e):
        proj = self.rows[int(e.row_key.value)]["project"]
        subset = [s for s in self.summaries if s.project == proj]
        self.app.push_screen(BrowserScreen(short_proj(proj, 46), summaries=subset))

    def action_all_sessions(self):
        if self.summaries:
            self.app.push_screen(BrowserScreen(
                f"all {len(self.summaries)} sessions", summaries=list(self.summaries)))

    def action_skills(self):
        self.app.push_screen(SkillScreen(root=self.root, scope="all projects"))

    def action_tools(self):
        if not self.summaries:
            return
        merged = {}
        for s in self.summaries:
            for nm, c in s.hist.items():
                merged[nm] = merged.get(nm, 0) + c
        self.app.push_screen(ToolsScreen("all projects", merged))

    def action_mcp(self):
        if self.summaries:
            self.app.push_screen(McpScreen("all projects", summaries=self.summaries))


# --------------------------------------------------------------------------- #
class BrowserScreen(Sortable, Screen):
    """A list of sessions — either a pre-scanned subset (one project / all), or
    scanned from `root` (used by --local). is_root=True means Esc quits."""
    BINDINGS = [("escape", "back", "Back"), ("q", "app.quit", "Quit"),
                ("s", "skills", "Skill regret"), ("t", "tools", "Tools"),
                ("m", "mcp", "MCP")]
    COLS = [
        ("$", lambda s: s.cost, True),
        ("out", lambda s: s.out, True),
        ("in+cache", lambda s: s.ctx_in, True),
        ("turns", lambda s: s.turns, True),
        ("wall", lambda s: s.wall, True),
        ("tok/s", lambda s: s.tok_per_s, True),
        ("model", lambda s: s.model, False),
        ("project", lambda s: s.project, False),
        ("when", lambda s: s.tmax or datetime.min, True),
    ]

    def __init__(self, title, summaries=None, root=None, is_root=False):
        super().__init__()
        self.title_ = title
        self.summaries = summaries or []
        self.scan_root = root          # if set, scan it on mount
        self.is_root = is_root
        self.sort_i, self.sort_rev = 0, True

    def compose(self):
        yield Header()
        self.status = Static("…", id="status")
        yield self.status
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = self.title_
        self.table.add_columns(*[c[0] for c in self.COLS])
        if self.scan_root is not None:
            self.status.update("Scanning transcripts…")
            self.load_data()
        else:
            self._ready()

    def action_back(self):
        if self.is_root:
            self.app.exit()
        else:
            self.app.pop_screen()

    @work(thread=True, exclusive=True)
    def load_data(self):
        rows = self.app.cached_corpus(self.scan_root)
        self.app.call_from_thread(self._set_rows, rows)

    def reload(self):
        if self.scan_root is not None:
            self.load_data()

    def _set_rows(self, rows):
        self.summaries = rows
        self._ready()

    def _ready(self):
        if self.summaries:
            total = sum(s.cost for s in self.summaries)
            est = (" (some est.)" if any(pricing.is_estimate(s.model)
                                         for s in self.summaries) else "")
            self.status.update(
                f"[b]{len(self.summaries)}[/b] sessions · ~[b]${total:,.0f}[/b] "
                f"token-value{est} · Enter opens · [b]s[/b]=skills [b]t[/b]=tools "
                f"[b]m[/b]=MCP · [b]1-9[/b]/click header=sort")
        else:
            self.status.update(
                "[yellow]No sessions here.[/yellow] For --local, run csa from a "
                "directory you've used with Claude Code.")
        self._fill()

    def _fill(self):
        self.summaries.sort(key=self.COLS[self.sort_i][1], reverse=self.sort_rev)
        self.table.clear()
        for i, s in enumerate(self.summaries):
            self.table.add_row(
                f"${s.cost:,.2f}", human(s.out), human(s.ctx_in), str(s.turns),
                f"{s.wall / 60:.0f}m", f"{s.tok_per_s:.1f}",
                (s.model or "?").split("[")[0].replace("claude-", ""),
                short_proj(s.project), when(s.tmax), key=str(i))

    def on_data_table_header_selected(self, e):
        i = e.column_index
        self.sort_rev = self.COLS[i][2] if i != self.sort_i else not self.sort_rev
        self.sort_i = i
        self._fill()

    def on_data_table_row_selected(self, e):
        self.app.push_screen(SessionScreen(self.summaries[int(e.row_key.value)]))

    def action_skills(self):
        if self.summaries:
            self.app.push_screen(SkillScreen(
                paths=[str(s.path) for s in self.summaries], scope=self.title_))

    def action_tools(self):
        if not self.summaries:
            return
        merged = {}
        for s in self.summaries:
            for nm, c in s.hist.items():
                merged[nm] = merged.get(nm, 0) + c
        self.app.push_screen(ToolsScreen(self.title_, merged))

    def action_mcp(self):
        if self.summaries:
            self.app.push_screen(McpScreen(self.title_, summaries=self.summaries))


    def action_tools(self):
        if not self.summaries:
            return
        merged = {}
        for s in self.summaries:
            for nm, c in s.hist.items():
                merged[nm] = merged.get(nm, 0) + c
        self.app.push_screen(ToolsScreen(self.title_, merged))

    def action_mcp(self):
        if self.summaries:
            self.app.push_screen(McpScreen(self.title_, summaries=self.summaries))


# --------------------------------------------------------------------------- #
class SessionScreen(Sortable, Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit"),
                ("g", "graphs", "Stats ⇄ graphs"),
                ("a", "all_turns", "All turns"), ("t", "tools", "Tools"),
                ("m", "mcp", "MCP"), ("b", "subagents", "Subagents")]
    COLS = [
        ("#", lambda t: t.index, False),
        ("gap", lambda t: t.gap, True),
        ("dur", lambda t: t.duration, True),
        ("out", lambda t: t.out, True),
        ("ctx", lambda t: t.ctx, True),
        ("$", lambda t: t.cost, True),
        ("tok/s", lambda t: t.tok_per_s, True),
        ("tools", lambda t: len(t.tools), True),
        ("fric", lambda t: t.friction, True),
        ("skills", lambda t: ",".join(sorted(t.skills)), False),
        ("prompt", lambda t: t.prompt or "", False),
    ]

    def __init__(self, summary):
        super().__init__()
        self.summary = summary
        self.session = None
        self.subs = []                # list[model.Subagent]
        self.subs_by_uid = {}         # Task tool_use id -> Subagent (for nested drill)
        self.all_turns, self.view = [], []
        self.bkts = []
        self.sort_i, self.sort_rev = 0, False
        self.filter = None        # (lo, hi) seconds, or None
        self.show_graphs = False  # stats panel is the default; g toggles graphs

    def compose(self):
        yield Header()
        self.head = Static("Loading session…", id="head")
        yield self.head
        self.panel = Static("", id="panel")
        yield self.panel
        self.bkt_table = DataTable(cursor_type="row", zebra_stripes=True, id="buckets")
        yield self.bkt_table
        self.turn_table = DataTable(cursor_type="row", zebra_stripes=True, id="turns")
        yield self.turn_table
        yield Footer()

    def on_mount(self):
        self.bkt_table.add_columns("when", "tokens", "spend", "turns")
        self.turn_table.add_columns(*[c[0] for c in self.COLS])
        self.bkt_table.display = False     # start on the stats panel; g toggles
        self.sub_title = short_proj(self.summary.project, 40)
        self.load_session()

    @work(thread=True, exclusive=True)
    def load_session(self):
        s = model.load_session(self.summary.path)
        subs = model.load_subagents(self.summary.path)
        self.app.call_from_thread(self._populate, s, subs)

    def _populate(self, s, subs):
        self.session = s
        self.subs = subs
        self.subs_by_uid = {sub.task_uid: sub for sub in subs if sub.task_uid}
        self.all_turns = list(s.turns)
        self.view = list(s.turns)
        self.bkts = s.buckets()
        flag = " (cost est.)" if pricing.is_estimate(s.model) else ""
        self.head.update(
            f"[b]{s.session_id[:8]}[/b] · {s.model or '?'} · {len(s.turns)} turns · "
            f"out {human(s.out)} · peak-ctx [b]{s.ctx_peak:,}[/b] · "
            f"[b]${s.cost:,.2f}[/b]{flag} · {s.tok_per_s:.0f} tok/s · "
            f"[dim]g=stats⇄graphs · a=all turns · t=tools · m=MCP · "
            f"b=subagents · Enter a turn for its commands[/dim]")
        self.panel.update(self._panel_text())
        self._fill_buckets()
        self._fill_turns()
        self.turn_table.focus()   # the visible table; bkt_table starts hidden

    def _panel_text(self):
        s, st = self.session, self.session.stats()
        skills = sorted(st["skills"].items(), key=lambda kv: -kv[1])
        sk_str = ", ".join(f"{k.split(':')[-1]}×{v}" for k, v in skills[:8]) or "none"
        mcp_serv = st.get("mcp_servers", {})
        mcp_str = ", ".join(f"{srv}×{n}" for srv, n in sorted(
            mcp_serv.items(), key=lambda kv: -kv[1])[:5]) or "none"
        so = sum(x.out for x in self.subs)
        sc = sum(x.cost for x in self.subs)
        sub_line = (
            f"[b]subagents[/b] {len(self.subs)} spawned · +{human(so)} out · "
            f"[b]+${sc:,.2f}[/b]  [dim](press [b]b[/b] — their cost is NOT in the "
            f"turn list / header above, which is the main thread only)[/dim]"
            if self.subs else "[dim]no subagents spawned this session[/dim]")
        return "\n".join([
            f"[b]started[/b] {when(s.start)}    [b]ended[/b] {when(s.end)}    "
            f"([b]{s.wall / 60:.0f}m[/b] elapsed wall-clock)",
            "",
            f"[b]turns[/b] {st['turns']}   [b]tool calls[/b] {st['tools']}   "
            f"[b]skill loads[/b] {st['skill_calls']}   [b]MCP calls[/b] {st['mcp']}  "
            f"[dim]({mcp_str})[/dim]   "
            f"[b]subagents[/b] {st['subagents']}   [b]asked you[/b] {st['asks']}",
            "",
            f"[yellow]friction[/yellow] {st['friction_turns']}/{st['turns']} turns  ·  "
            f"corrections {st['corrections']}  ·  walkbacks {st['walkbacks']}  ·  "
            f"self-corrections {st['self_corrections']}  ·  "
            f"error-turns {st['error_turns']} ({st['tool_errors']} tool errors)  ·  "
            f"retry-loops {st['loops']}  [dim](suspicion, not proof)[/dim]",
            "",
            sub_line,
            "",
            f"[b]skills used[/b]: {sk_str}",
            "",
            "[dim]press [b]g[/b] for the time-bucketed graphs · [b]t[/b] for the tools "
            "histogram · [b]m[/b] for MCP servers · Enter a turn below to drill in[/dim]",
        ])

    def _fill_buckets(self):
        b = self.bkts
        mt = max((x["tok"] for x in b), default=0)
        mc = max((x["cost"] for x in b), default=0)
        mn = max((x["turns"] for x in b), default=0)
        self.bkt_table.clear()
        for i, x in enumerate(b):
            self.bkt_table.add_row(
                x["at"].strftime("%m-%d %H:%M"),
                f"{_bar(x['tok'], mt)} {human(x['tok'])}",
                f"{_bar(x['cost'], mc)} ${x['cost']:.2f}",
                f"{_bar(x['turns'], mn)} {x['turns']}",
                key=str(i))

    def _resort(self):
        self._fill_turns()

    def _fill_turns(self):
        self.view.sort(key=self.COLS[self.sort_i][1], reverse=self.sort_rev)
        self.turn_table.clear()
        for i, t in enumerate(self.view):
            fr = "".join(c for c, x in [("C", t.correction), ("W", t.walkback),
                                        ("S", t.self_correct),
                                        ("E", t.tool_errors >= 2),
                                        ("L", t.looped)] if x) or "·"
            sk = ",".join(sorted(x.split(":")[-1] for x in t.skills)) or "-"
            self.turn_table.add_row(
                str(t.index), f"{t.gap:.0f}s", f"{t.duration:.0f}s", human(t.out),
                human(t.ctx), f"${t.cost:,.2f}", f"{t.tok_per_s:.0f}",
                str(len(t.tools)), fr, sk[:22], (t.prompt or "")[:64], key=str(i))

    def action_graphs(self):
        self.show_graphs = not self.show_graphs
        self.panel.display = not self.show_graphs
        self.bkt_table.display = self.show_graphs
        # keep focus on a visible table so ←/→ act on what the user sees
        (self.bkt_table if self.show_graphs else self.turn_table).focus()

    def action_all_turns(self):
        self.filter = None
        self.view = list(self.all_turns)
        self.sub_title = short_proj(self.summary.project, 40)
        self._fill_turns()

    def action_tools(self):
        if not self.session:
            return
        hist, toks = {}, {}
        for t in self.all_turns:
            for c in t.tools:
                hist[c.name] = hist.get(c.name, 0) + 1
                toks[c.name] = toks.get(c.name, 0) + c.out
        self.app.push_screen(ToolsScreen(self.session.session_id[:8], hist, toks))

    def action_mcp(self):
        if self.session:
            self.app.push_screen(McpScreen(self.session.session_id[:8],
                                           session=self.session))

    def action_subagents(self):
        if not self.session:
            return
        if not self.subs:
            self.notify("no subagents spawned this session")
            return
        self.app.push_screen(SubagentsScreen(self.session.session_id[:8], self.subs))

    def on_data_table_header_selected(self, e):
        if e.data_table is not self.turn_table:
            return
        i = e.column_index
        self.sort_rev = self.COLS[i][2] if i != self.sort_i else not self.sort_rev
        self.sort_i = i
        self._fill_turns()

    def on_data_table_row_selected(self, e):
        if e.data_table is self.bkt_table:
            x = self.bkts[int(e.row_key.value)]
            base = self.session.start
            self.view = [t for t in self.all_turns
                         if x["lo"] <= (t.start - base).total_seconds() < x["hi"]]
            self.filter = (x["lo"], x["hi"])
            self.sub_title = (f"{short_proj(self.summary.project, 26)} · "
                              f"{x['at'].strftime('%m-%d %H:%M')} ({len(self.view)} turns)")
            self._fill_turns()
        else:
            self.app.push_screen(TurnScreen(self.session, self.view[int(e.row_key.value)],
                                            subagents=self.subs_by_uid))


# --------------------------------------------------------------------------- #
class TurnScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]

    def __init__(self, session, turn, subagents=None):
        super().__init__()
        self.session = session
        self.turn = turn
        self.subagents = subagents or {}   # Task tool_use id -> Subagent

    def compose(self):
        t = self.turn
        yield Header()
        fr = [name for name, x in [("user-correction-next", t.correction),
                                    ("self-correction", t.self_correct),
                                    ("user-walkback-next", t.walkback),
                                    (f"{t.tool_errors} tool-error(s)", t.tool_errors >= 2),
                                    ("tool-loop", t.looped)] if x]
        fr_line = ("[yellow]friction (suspicion, not proof): " + ", ".join(fr)
                   + "[/yellow]") if fr else "[green]no friction flags[/green]"
        # wall-time breakdown
        exec_s, you_s, think_s, _ = t.time_breakdown
        bd = (f"time [b]{t.duration:.0f}s[/b] = "
              f"exec [b]{exec_s:.0f}s[/b] · you [b]{you_s:.0f}s[/b] · "
              f"model-think [b]{think_s:.0f}s[/b]")
        prompt = (t.prompt or "").strip() or "(no text prompt)"
        # friction evidence: show the actual pushback text so you can see WHY
        err_calls = [c for c in t.tools if c.is_error]
        err_line = ""
        if err_calls:
            err_line = (f"\n[red]✗ {len(err_calls)} failing call(s):[/red] "
                        + ", ".join(c.name for c in err_calls[:3])
                        + (" …" if len(err_calls) > 3 else "")
                        + "  [dim](Enter to read the error)[/dim]")
        walk_line = ""
        if t.walkback_text:
            walk_line = (f"\n[yellow]next user pivoted:[/yellow] "
                         f"[dim]\"{t.walkback_text[:140]}\"[/dim]")
        corr_line = ""
        if t.correction_text:
            corr_line = (f"\n[yellow]next user pushed back:[/yellow] "
                         f"[dim]\"{t.correction_text[:140]}\"[/dim]")
        head = (f"[b]Turn {t.index}[/b] · gap {t.gap:.0f}s · dur [b]{t.duration:.0f}s[/b] · "
                f"in {human(t.fresh)} / out [b]{human(t.out)}[/b] tok · ctx {t.ctx:,} · "
                f"[b]${t.cost:,.2f}[/b] · {t.tok_per_s:.0f} tok/s\n"
                f"skills: {', '.join(sorted(t.skills)) or '-'}\n"
                f"{bd}  [dim](you = time waiting on AskUserQuestion)[/dim]\n"
                f"{fr_line}{err_line}{corr_line}{walk_line}\n"
                f"[dim]exec = tool run · wall = call→next step · Δ = model think + "
                f"idle after (AskUserQuestion exec = you answering) · "
                f"Enter a command for its full input + result "
                f"(↳ = Task: drills into the subagent)[/dim]\n\n"
                f"[b]prompt[/b]: {prompt[:300]}")
        yield VerticalScroll(Static(head))
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = f"turn {self.turn.index} commands"
        self.table.add_columns("#", "tool", "exec", "wall", "Δ", "summary")
        for i, c in enumerate(self.turn.tools):
            mark = " ✗" if c.is_error else ""
            sub = " ↳" if c.uid in self.subagents else ""   # drills into the subagent
            delta = max(0.0, c.wall - c.dur)
            self.table.add_row(str(i + 1), c.name + mark + sub, f"{c.dur:.0f}s",
                               f"{c.wall:.0f}s", f"{delta:.0f}s", c.summary or "",
                               key=str(i))
        if not self.turn.tools:
            self.table.add_row("-", "(no tool calls)", "", "", "", "")
        self.table.focus()   # not the VerticalScroll header, so ←/→ act on the rows

    def on_data_table_row_selected(self, e):
        if e.row_key.value is None:
            return
        idx = int(e.row_key.value)
        if not (0 <= idx < len(self.turn.tools)):
            return
        c = self.turn.tools[idx]
        sub = self.subagents.get(c.uid)
        if sub and sub.turn:            # a Task call -> drill into the subagent's commands
            self.app.push_screen(TurnScreen(sub.session, sub.turn))
        else:
            self.app.push_screen(StepScreen(c))


# --------------------------------------------------------------------------- #
class StepScreen(Screen):
    """Full detail of one command/step: its complete input and (capped) result."""
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]

    def __init__(self, call):
        super().__init__()
        self.call = call

    def compose(self):
        c = self.call
        err = " [red]✗ error[/red]" if c.is_error else ""
        out_note = (f" · out ~{c.out} tok [dim](per-response, overlaps if "
                    f"this response emitted several tools)[/dim]") if c.out else ""
        srv, tool = model.parse_mcp(c.name)
        mcp_note = (f" [dim](mcp server: {srv})[/dim]") if srv else ""
        head = (f"[b]{c.name}[/b]{mcp_note}{err} · exec [b]{c.dur:.0f}s[/b] · wall "
                f"{c.wall:.0f}s · Δ {max(0.0, c.wall - c.dur):.0f}s{out_note}")
        try:
            inp = json.dumps(c.input, indent=2, ensure_ascii=False) if c.input else "(no input)"
        except (TypeError, ValueError):
            inp = str(c.input)
        body = (f"INPUT\n{'─' * 60}\n{inp[:8000]}\n\n"
                f"RESULT  (capped)\n{'─' * 60}\n{c.result or '(no result captured)'}")
        yield Header()
        yield Static(head, id="head")
        yield VerticalScroll(Static(body, markup=False, id="step"))
        yield Footer()

    def on_mount(self):
        self.sub_title = f"{self.call.name} — full step"


# --------------------------------------------------------------------------- #
class SubagentsScreen(Screen):
    """The subagents a session spawned — each a mini-transcript you can drill into.

    Their cost lives here, not in the parent's turn list (the session header is
    the main thread only). Enter a row to see that subagent's tool calls.
    """
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]

    def __init__(self, title, subs):
        super().__init__()
        self.title_ = title
        self.subs = subs

    def compose(self):
        tot_out = sum(s.out for s in self.subs)
        tot_cost = sum(s.cost for s in self.subs)
        yield Header()
        yield Static(f"[b]{len(self.subs)} subagents[/b] · out {human(tot_out)} · "
                     f"[b]${tot_cost:,.2f}[/b]  "
                     f"[dim](Enter a subagent for its tool calls)[/dim]", id="head")
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = f"{self.title_} subagents"
        self.table.add_columns("#", "agent", "model", "task", "tools", "out", "$", "dur")
        for i, s in enumerate(self.subs):
            label = s.task_desc or (s.turn.prompt if s.turn else "") or "(no label)"
            mdl = (s.model or "?").replace("claude-", "")
            n_tools = len(s.turn.tools) if s.turn else 0
            self.table.add_row(
                str(i + 1), s.agent_id[:10], mdl[:12], label[:40], str(n_tools),
                human(s.out), f"${s.cost:,.2f}", f"{s.dur:.0f}s", key=str(i))

    def on_data_table_row_selected(self, e):
        if e.row_key.value is None:
            return
        s = self.subs[int(e.row_key.value)]
        if s.turn:
            self.app.push_screen(TurnScreen(s.session, s.turn))


# --------------------------------------------------------------------------- #
class SkillScreen(Sortable, Screen):
    """Corpus-wide per-skill regret leaderboard. Suspicion, not proof."""
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]
    COLS = [
        ("skill", lambda r: r[0], False),
        ("turns", lambda r: r[1]["fires"], True),
        ("out", lambda r: r[1]["out"], True),
        ("tools", lambda r: r[1]["tools"], True),
        ("asks", lambda r: r[1]["asks"], True),
        ("regret%", lambda r: (r[1]["regret_turns"] / r[1]["fires"] if r[1]["fires"] else 0), True),
    ]

    def __init__(self, root=None, paths=None, scope=""):
        super().__init__()
        self.root = root
        self.paths = paths
        self.scope = scope
        self.rows = []
        self.sort_i, self.sort_rev = 2, True  # default: out desc

    def compose(self):
        yield Header()
        self.status = Static("Loading sessions for regret analysis…", id="status")
        yield self.status
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = f"skill regret · {self.scope}" if self.scope else "skill regret"
        self.table.add_columns(*[c[0] for c in self.COLS])
        self.load()

    @work(thread=True, exclusive=True)
    def load(self):
        def prog(n, total):
            if n % 150 == 0 or n == total:
                self.app.call_from_thread(self.status.update,
                                          f"Analyzing… {n}/{total} sessions")
        agg = model.scan_skill_regret(root=self.root, paths=self.paths, progress=prog)
        self.app.call_from_thread(self._populate, agg)

    def _populate(self, agg):
        self.rows = list(agg.items())
        # show how many skills are low-sample so the leaderboard is read with
        # the right skepticism
        low = sum(1 for _, a in self.rows if a["fires"] < 5)
        low_note = (f" · [dim]{low} skill{'s' if low != 1 else ''} fired <5× "
                    f"(regret% dimmed, sunk in sort)[/dim]") if low else ""
        self.status.update(
            "[b]turns[/b]=turns the skill ran · [b]tools[/b]=tool calls it triggered · "
            "[b]asks[/b]=times it asked YOU a question · [b]regret%[/b]=turns with "
            "friction [dim](correlation, not proof)[/dim]. Enter a skill to see "
            "WHERE its friction came from (corrections vs walkbacks vs tool "
            "errors)." + low_note)
        self._fill()

    def _fill(self):
        # Low-sample rows (n < 5) sink to the bottom when sorting by regret% so
        # the scary "100%" from one-shot skills stops dominating the leaderboard.
        key = self.COLS[self.sort_i][1]
        rows = list(self.rows)
        if self.sort_i == 5:                                # regret% column
            rows.sort(key=lambda r: (r[1]["fires"] < 5, -key(r), r[0]),
                      reverse=not self.sort_rev)
        else:
            rows.sort(key=key, reverse=self.sort_rev)
        self.table.clear()
        for i, (sk, a) in enumerate(rows):
            fires = a["fires"]
            pct = 100 * a["regret_turns"] / fires if fires else 0
            if fires < 5:
                pct_s = "[dim]n<5[/dim]"
            else:
                pct_s = f"{pct:.0f}%"
            self.table.add_row(
                sk, str(fires), human(a["out"]), str(a["tools"]),
                str(a["asks"]), pct_s, key=str(i))

    def on_data_table_header_selected(self, e):
        i = e.column_index
        self.sort_rev = self.COLS[i][2] if i != self.sort_i else not self.sort_rev
        self.sort_i = i
        self._fill()

    def on_data_table_row_selected(self, e):
        sk, a = self.rows[int(e.row_key.value)]
        self.app.push_screen(SkillDetailScreen(sk, a))


# --------------------------------------------------------------------------- #
class SkillDetailScreen(Screen):
    """What a skill ACTUALLY does, observed from the traces (not its SKILL.md)."""
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]

    def __init__(self, skill, data):
        super().__init__()
        self.skill = skill
        self.data = data

    def compose(self):
        a = self.data
        fires = a["fires"] or 1
        per_turn = a["tools"] / fires
        asks_per = a["asks"] / fires
        pct = 100 * a["regret_turns"] / fires
        pct_s = "[dim]n<5[/dim]" if a["fires"] < 5 else f"{pct:.0f}%"
        ask_note = ""
        if a["asks"]:
            ask_note = (f"\n[yellow]⚠ asked YOU a question {a['asks']} times "
                        f"({asks_per:.1f}/turn) — a skill that interrupts to ask for "
                        "extra input slows you down, often unnoticed.[/yellow]")
        inj = a.get("injections", 0)
        inj_line = ""
        if inj:
            avg = a["inject_chars"] / inj
            heavy = "  [red](heavy!)[/red]" if avg / 4 > 30000 else ""
            inj_line = (f"\n[b]context weight[/b]: loads ~{avg / 1024:.1f} KB "
                        f"(~{avg / 4:,.0f} tok, est) into context each time it runs"
                        f" · {inj} load{'s' if inj != 1 else ''} seen{heavy}")
        secs = a.get("secs", 0.0)
        tstr = f"{secs / 60:.0f}m" if secs >= 120 else f"{secs:.0f}s"
        # friction breakdown — show WHERE the regret came from so 100% from one
        # correction is read as different from 100% from twelve tool errors
        corr = a.get("corrections", 0)
        wlk = a.get("walkbacks", 0)
        sc = a.get("self_corrections", 0)
        te = a.get("tool_errors", 0)
        et = a.get("error_turns", 0)
        loops = a.get("loops", 0)
        rt = a["regret_turns"]
        bd = []
        if corr:
            bd.append(f"user-correction [b]{corr}[/b]")
        if wlk:
            bd.append(f"user-walkback [b]{wlk}[/b]")
        if sc:
            bd.append(f"self-correction [b]{sc}[/b]")
        if te:
            bd.append(f"tool-errors [b]{te}[/b] ({et} turn{'s' if et != 1 else ''})")
        if loops:
            bd.append(f"retry-loops [b]{loops}[/b]")
        bd_line = ("\n[b]friction breakdown[/b]: " + " · ".join(bd)
                   if bd else "\n[b]friction breakdown[/b]: [green]none[/green]")
        if rt and bd:
            # caveat when sum of components > regret_turns (turns can have more
            # than one friction type — note this honestly rather than gaming it)
            if corr + wlk + sc + et + loops > rt:
                bd_line += ("\n[dim](turns can carry multiple friction types, so "
                            "components may exceed regret-turns)[/dim]")
        head = (f"[b]{self.skill}[/b]\n"
                f"ran in [b]{a['fires']}[/b] turns · spent [b]{tstr}[/b] "
                f"({secs / fires:.0f}s/turn) · generated [b]{human(a['out'])}[/b] "
                f"output tok · triggered [b]{a['tools']}[/b] tool calls "
                f"({per_turn:.1f}/turn) · friction in {pct_s} of its turns "
                f"[dim](suspicion)[/dim]{bd_line}{inj_line}{ask_note}\n\n"
                f"[b]What it actually triggers[/b] — calls · exec vs wall "
                f"[dim](wall−exec = model think after; AskUserQuestion exec = "
                f"you answering)[/dim]:")
        yield Header()
        yield Static(head, id="head")
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = "what this skill really does"
        self.table.add_columns("tool", "calls", "exec", "wall", "out tok", "% of its tool use")
        hist = self.data["hist"]
        total = sum(h["calls"] for h in hist.values()) or 1
        for name, h in sorted(hist.items(), key=lambda kv: kv[1]["calls"],
                              reverse=True):
            self.table.add_row(name, str(h["calls"]), f"{h['secs']:.0f}s",
                               f"{h.get('wall', 0):.0f}s",
                               human(h.get("out", 0)) if h.get("out") else "-",
                               f"{100 * h['calls'] / total:.0f}%")
        if not self.data["hist"]:
            self.table.add_row("(no tool calls)", "0", "0s", "0s", "-", "-")


# --------------------------------------------------------------------------- #
class ToolsScreen(Sortable, Screen):
    """Which tools were called and how often — for a session or a group."""
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]
    COLS = [("tool", lambda r: r[0], False),
            ("calls", lambda r: r[1], True),
            ("share", lambda r: r[1], True)]

    def __init__(self, scope, hist, tokens=None):
        super().__init__()
        self.scope = scope
        self.rows = list(hist.items())
        self.total = sum(hist.values()) or 1
        # tokens: tool_name -> out-tokens (per-response attribution, can overlap
        # across tools emitted in the same response — labeled in the UI)
        self.tokens = tokens or {}
        self.sort_i, self.sort_rev = 1, True

    def compose(self):
        yield Header()
        yield Static(f"[b]Tools — {self.scope}[/b] · [b]{self.total:,}[/b] tool calls "
                     f"across {len(self.rows)} tool types · click a header to sort · "
                     f"out tokens per-response (overlap if one response emits "
                     f"several tools)",
                     id="head")
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = "tools called"
        cols = ["tool", "calls", "out tok", "% of all calls"]
        self.table.add_columns(*cols)
        self._fill()

    def _fill(self):
        self.rows.sort(key=self.COLS[self.sort_i][1], reverse=self.sort_rev)
        self.table.clear()
        m = max((c for _, c in self.rows), default=1)
        for name, c in self.rows:
            tok = self.tokens.get(name, 0)
            self.table.add_row(name, f"{c:,}", human(tok) if tok else "-",
                               f"{_bar(c, m, 8)} {100 * c / self.total:.1f}%")

    def on_data_table_header_selected(self, e):
        i = e.column_index
        self.sort_rev = self.COLS[i][2] if i != self.sort_i else not self.sort_rev
        self.sort_i = i
        self._fill()


# --------------------------------------------------------------------------- #
class FileScreen(Sortable, Screen):
    """Which files got read/edited/written most across the logs, with an on-disk
    size estimate. 'reads' = tool calls naming that file_path (Read/Edit/Write/
    NotebookEdit/MultiEdit + any MCP tool using file_path). Size/tokens are the
    file's CURRENT on-disk size (~bytes/4) — it may have changed or been deleted
    since the session, so this is an estimate, not what was actually in context."""
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]
    COLS = [
        ("file",   lambda r: r[0], False),
        ("reads",  lambda r: r[1]["ops"]["reads"], True),
        ("edits",  lambda r: r[1]["ops"]["edits"], True),
        ("writes", lambda r: r[1]["ops"]["writes"], True),
        ("other",  lambda r: r[1]["ops"]["other"], True),
        ("total",  lambda r: r[1]["total"], True),
        ("~size",  lambda r: r[1]["size"] or 0, True),
        ("~tokens",lambda r: r[1]["size"] or 0, True),
    ]

    def __init__(self, scope, root, summaries=None):
        super().__init__()
        self.scope = scope
        self.root = root
        self._summaries = summaries  # if set, skip scan; scope to these sessions
        self.rows = []                          # list[(path, {"ops", "total", "size"})]
        self.total = 1
        self.sort_i, self.sort_rev = 5, True     # total desc

    def compose(self):
        yield Header()
        self.status = Static("Scanning transcripts for file access…", id="status")
        yield self.status
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = f"files · {self.scope}"
        self.table.add_columns("file", "reads", "edits", "writes", "other", "total", "~size", "~tok×ops")
        self.load()

    def reload(self):
        self.rows = []
        self.table.clear()
        self.status.update("Scanning transcripts for file access…")
        self.load()

    @work(thread=True, exclusive=True)
    def load(self):
        merged = {}   # path -> {"ops": {...}, "sessions": [(summary, ops)]}
        source = self._summaries if self._summaries is not None else self.app.cached_corpus(self.root)
        for s in source:
            for path, ops in s.file_hist.items():
                e = merged.setdefault(path, {
                    "ops": {"reads": 0, "edits": 0, "writes": 0, "other": 0},
                    "sessions": [],
                })
                for k in e["ops"]:
                    e["ops"][k] += ops.get(k, 0)
                e["sessions"].append((s, ops))
        rows = []
        for path, d in merged.items():
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
            total = sum(d["ops"].values())
            rows.append((path, {"ops": d["ops"], "total": total, "size": size,
                                 "sessions": d["sessions"]}))
        self.app.call_from_thread(self._populate, rows)

    def _populate(self, rows):
        self.rows = rows
        self.total = sum(r[1]["total"] for r in rows) or 1
        gone = sum(1 for r in rows if r[1]["size"] is None)
        tot_tok = sum(r[1]["size"] for r in rows if r[1]["size"] is not None) / 4
        gone_note = (f" · [dim]{gone} no longer on disk[/dim]") if gone else ""
        if not rows:
            self.status.update("[yellow]No file reads/edits recorded in this scope."
                               "[/yellow]")
        else:
            self.status.update(
                f"[b]{len(rows)}[/b] files · [b]{self.total:,}[/b] total accesses · "
                f"~[b]{human(int(tot_tok))}[/b] tok on-disk content "
                f"[dim](est, bytes/4 × ops; file may have changed since)[/dim]{gone_note} · "
                f"[b]1-9[/b]/click header=sort")
        self._fill()

    def _fill(self):
        self.rows.sort(key=self.COLS[self.sort_i][1], reverse=self.sort_rev)
        self.table.clear()
        m = max((r[1]["total"] for r in self.rows), default=1)
        for path, d in self.rows:
            ops, total, size = d["ops"], d["total"], d["size"]
            tok = "?" if size is None else human(int(size / 4 * total))
            self.table.add_row(
                short_file(path),
                str(ops["reads"]) if ops["reads"] else "-",
                str(ops["edits"]) if ops["edits"] else "-",
                str(ops["writes"]) if ops["writes"] else "-",
                str(ops["other"]) if ops["other"] else "-",
                f"{_bar(total, m, 8)} {total:,}",
                human_bytes(size), tok)

    def on_data_table_header_selected(self, e):
        i = e.column_index
        self.sort_rev = self.COLS[i][2] if i != self.sort_i else not self.sort_rev
        self.sort_i = i
        self._fill()

    def on_data_table_row_selected(self, e):
        if e.row_key.value is None:
            return
        path, d = self.rows[int(e.row_key.value)]
        self.app.push_screen(FileSessionScreen(path, d["sessions"], d["size"]))


# --------------------------------------------------------------------------- #
class FileSessionScreen(Sortable, Screen):
    """Per-session breakdown for one file: when it was accessed, by which project,
    and how many reads/edits/writes per session. Enter a session to open it."""
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]
    COLS = [
        ("date",   lambda r: r[0].tmax or datetime.min, True),
        ("project",lambda r: r[0].project, False),
        ("reads",  lambda r: r[1].get("reads", 0), True),
        ("edits",  lambda r: r[1].get("edits", 0), True),
        ("writes", lambda r: r[1].get("writes", 0), True),
        ("other",  lambda r: r[1].get("other", 0), True),
        ("total",  lambda r: sum(r[1].values()), True),
        ("~tok×ops", lambda r: sum(r[1].values()), True),  # placeholder; filled in _fill
    ]

    def __init__(self, path, sessions, size):
        super().__init__()
        self.path = path
        self.sessions = sessions   # list[(SessionSummary, ops_dict)]
        self.size = size
        self.sort_i, self.sort_rev = 0, True   # date desc

    def compose(self):
        yield Header()
        size_s = human_bytes(self.size)
        tok_note = (f" · ~{human(int(self.size / 4))} tok/read"
                    if self.size is not None else " · size unknown (gone)")
        yield Static(
            f"[b]{short_file(self.path)}[/b]  {size_s}{tok_note}\n"
            f"[dim]{len(self.sessions)} session{'s' if len(self.sessions) != 1 else ''} "
            f"accessed this file · Enter a session to open it · "
            f"[b]1-9[/b]/click header=sort[/dim]",
            id="head")
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = short_file(self.path, 40)
        self.table.add_columns("date", "project", "reads", "edits", "writes",
                               "other", "total", "~tok×ops")
        self._fill()

    def _fill(self):
        key = self.COLS[self.sort_i][1]
        self.sessions.sort(key=lambda r: key(r), reverse=self.sort_rev)
        self.table.clear()
        tok_per_read = int(self.size / 4) if self.size is not None else None
        for i, (s, ops) in enumerate(self.sessions):
            total = sum(ops.values())
            tok = "?" if tok_per_read is None else human(tok_per_read * total)
            self.table.add_row(
                when(s.tmax), short_proj(s.project, 36),
                str(ops.get("reads", 0)) if ops.get("reads") else "-",
                str(ops.get("edits", 0)) if ops.get("edits") else "-",
                str(ops.get("writes", 0)) if ops.get("writes") else "-",
                str(ops.get("other", 0)) if ops.get("other") else "-",
                str(total), tok, key=str(i))

    def on_data_table_header_selected(self, e):
        i = e.column_index
        self.sort_rev = self.COLS[i][2] if i != self.sort_i else not self.sort_rev
        self.sort_i = i
        self._fill()

    def on_data_table_row_selected(self, e):
        if e.row_key.value is None:
            return
        summary, _ = self.sessions[int(e.row_key.value)]
        self.app.push_screen(SessionScreen(summary))


# --------------------------------------------------------------------------- #
class McpScreen(Screen):
    """MCP servers -> their tools. Two-level: list of servers on entry, drill in
    to see per-tool counts and out-tokens for that server."""
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]

    def __init__(self, scope, summaries=None, session=None):
        super().__init__()
        self.scope = scope
        self.summaries = summaries or []
        self.session = session  # if set, use session-level data (has per-tool out)

    def _aggregate(self):
        """Returns ({server: calls}, {server: out_tokens}, {server: {tool: calls}})"""
        calls, outs, by_tool = {}, {}, {}
        if self.session:
            for t in self.session.turns:
                for c in t.tools:
                    srv, tool = model.parse_mcp(c.name)
                    if not srv:
                        continue
                    calls[srv] = calls.get(srv, 0) + 1
                    outs[srv] = outs.get(srv, 0) + c.out
                    by_tool.setdefault(srv, {})
                    by_tool[srv][tool] = by_tool[srv].get(tool, 0) + 1
        else:
            for s in self.summaries:
                for nm, c in s.hist.items():
                    srv, _ = model.parse_mcp(nm)
                    if srv:
                        calls[srv] = calls.get(srv, 0) + c
        return calls, outs, by_tool

    def compose(self):
        calls, outs, by_tool = self._aggregate()
        total_calls = sum(calls.values())
        yield Header()
        if total_calls == 0:
            yield Static("[yellow]No MCP calls recorded in this scope.[/yellow]\n\n"
                         "[dim]MCP tools are namespaced as "
                         "[b]mcp__<server>__<tool>[/b]. If you're running MCP "
                         "servers, their calls show up here grouped by server.[/dim]",
                         id="head")
        else:
            top = max(calls.values())
            server_lines = []
            for srv, n in sorted(calls.items(), key=lambda kv: -kv[1]):
                bar = _bar(n, top, 10)
                tok = human(outs.get(srv, 0)) if outs else "-"
                n_tool = len(by_tool.get(srv, {}))
                server_lines.append(
                    f"  [b]{srv}[/b]  {bar} [b]{n}[/b] call{'s' if n != 1 else ''}"
                    f"  · {n_tool} tool{'s' if n_tool != 1 else ''}"
                    f"  · out [b]{tok}[/b] tok"
                )
            head = (f"[b]MCP servers — {self.scope}[/b] · "
                    f"[b]{total_calls}[/b] calls across "
                    f"[b]{len(calls)}[/b] server{'s' if len(calls) != 1 else ''}"
                    f"\n" + "\n".join(server_lines))
            yield Static(head, id="head")
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = "MCP servers"
        self.table.add_columns("server", "calls", "out tok", "share")
        calls, outs, _ = self._aggregate()
        total = sum(calls.values()) or 1
        top = max(calls.values(), default=1)
        for srv, n in sorted(calls.items(), key=lambda kv: -kv[1]):
            self.table.add_row(srv, f"{n}", human(outs.get(srv, 0)) if outs else "-",
                               f"{_bar(n, top, 8)} {100 * n / total:.1f}%")
        if not calls:
            self.table.add_row("(no MCP calls)", "-", "-", "-")


# --------------------------------------------------------------------------- #
class ClaudeTraceApp(App):
    # ← / → are browser-style history back/forward (priority=True beats DataTable).
    # Enter / click drill into rows. r=refresh cache. f=file histogram.
    BINDINGS = [
        Binding("right", "nav_fwd", "→", priority=True, show=False),
        Binding("left",  "nav_back", "←", priority=True, show=False),
        Binding("f", "files", "Files"),
        Binding("r", "refresh", "Refresh"),
    ]
    CSS = """
    #status { height: auto; color: $text-muted; padding: 0 1; }
    #head { height: auto; padding: 0 1; }
    #panel { height: auto; padding: 1 1; border-bottom: solid $primary; }
    #step { padding: 0 1; }
    #buckets { height: 40%; border-bottom: solid $primary; }
    DataTable { height: 1fr; }
    """
    TITLE = "csa"

    def __init__(self, root, local_root=None):
        super().__init__()
        self.root = root
        self.local_root = local_root
        self._fwd: list = []          # browser-style forward history
        self._corpus_cache: dict = {} # root str -> [SessionSummary]; wiped by r
        self._corpus_lock = threading.Lock()

    def cached_corpus(self, root) -> list:
        """Return cached scan_corpus(root), scanning if needed. Thread-safe."""
        key = str(root)
        with self._corpus_lock:
            if key in self._corpus_cache:
                return self._corpus_cache[key]
        rows = model.scan_corpus(root)
        with self._corpus_lock:
            self._corpus_cache[key] = rows
        return rows

    def push_screen(self, screen, *args, **kwargs):
        """Any new navigation clears forward history (like a browser)."""
        self._fwd.clear()
        return super().push_screen(screen, *args, **kwargs)

    def on_mount(self):
        if self.local_root is not None:
            self.push_screen(BrowserScreen("this directory",
                                           root=self.local_root, is_root=True))
        else:
            self.push_screen(ProjectsScreen(self.root))

    def action_nav_back(self):
        """← : go back; save current screen for forward navigation."""
        if len(self.screen_stack) > 2:
            self._fwd.append(self.screen)
            self.pop_screen()

    def action_nav_fwd(self):
        """→ : go forward through history (if any), otherwise no-op."""
        if self._fwd:
            # bypass our push_screen override so we don't clear remaining _fwd
            Screen = self._fwd.pop()
            super().push_screen(Screen)

    def action_refresh(self):
        """r : clear corpus cache and reload current screen's data."""
        self._corpus_cache.clear()
        if hasattr(self.screen, "reload"):
            self.screen.reload()

    def action_files(self):
        """f : file histogram scoped to the current context (session/project/all)."""
        if isinstance(self.screen, FileScreen):
            return
        screen = self.screen
        root = self.local_root if self.local_root is not None else self.root
        if isinstance(screen, SessionScreen):
            self.push_screen(FileScreen(screen.session.session_id[:8],
                                        root, summaries=[screen.session]))
        elif isinstance(screen, BrowserScreen) and screen.summaries:
            self.push_screen(FileScreen(screen.title_, root,
                                        summaries=screen.summaries))
        else:
            scope = "this directory" if self.local_root is not None else "all projects"
            self.push_screen(FileScreen(scope, root))


def run(root, local_root=None):
    ClaudeTraceApp(root, local_root).run()
