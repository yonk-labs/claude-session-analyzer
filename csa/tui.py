"""Interactive TUI for browsing Claude Code usage.

Drill-down screens, each an aggregation of the same parsed model:
  Browser  -> sessions under a root (sortable: $, tokens, time, tok/s)
  Session  -> bucketed bar table (tokens/spend/turns) + sortable turns;
              click a bucket to filter turns to that time window
  Turn     -> the commands/tool-calls in one turn, with friction flags
  Skills   -> corpus-wide per-skill regret leaderboard (press 's' in browser)

Honest labels (abe review): tok/s is END-TO-END throughput, not decode speed;
friction/regret is suspicion, not proof of harm.
"""
from datetime import datetime

from textual import work
from textual.app import App
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
    return p.replace("-home-yonk-", "~/")[:w]


def _bar(val, maxv, width=10):
    n = int(round(val / maxv * width)) if maxv else 0
    return "█" * n + " " * (width - n)


# --------------------------------------------------------------------------- #
class BrowserScreen(Screen):
    BINDINGS = [("q", "app.quit", "Quit"), ("r", "reload", "Reload"),
                ("s", "skills", "Skill regret"), ("t", "tools", "Tools")]
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

    def __init__(self, root):
        super().__init__()
        self.root = root
        self.summaries = []
        self.sort_i, self.sort_rev = 0, True

    def compose(self):
        yield Header()
        self.status = Static("Scanning transcripts…", id="status")
        yield self.status
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.table.add_columns(*[c[0] for c in self.COLS])
        self.load_data()

    def action_reload(self):
        self.status.update("Rescanning…")
        self.load_data()

    def action_skills(self):
        self.app.push_screen(SkillScreen(self.root))

    def action_tools(self):
        if not self.summaries:
            return
        merged = {}
        for s in self.summaries:
            for nm, c in s.hist.items():
                merged[nm] = merged.get(nm, 0) + c
        self.app.push_screen(ToolsScreen(f"{len(self.summaries)} sessions", merged))

    @work(thread=True, exclusive=True)
    def load_data(self):
        rows = model.scan_corpus(self.root)
        self.app.call_from_thread(self._populate, rows)

    def _populate(self, rows):
        self.summaries = rows
        total = sum(s.cost for s in rows)
        est = " (some est.)" if any(pricing.is_estimate(s.model) for s in rows) else ""
        self.status.update(
            f"[b]{len(rows)}[/b] sessions · ~[b]${total:,.0f}[/b] token-value{est} · "
            f"click header to sort · Enter opens · [b]s[/b]=skill regret")
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


# --------------------------------------------------------------------------- #
class SessionScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit"),
                ("a", "all_turns", "All turns"), ("t", "tools", "Tools")]
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
    ]

    def __init__(self, summary):
        super().__init__()
        self.summary = summary
        self.session = None
        self.all_turns, self.view = [], []
        self.bkts = []
        self.sort_i, self.sort_rev = 0, False
        self.filter = None  # (lo, hi) seconds, or None

    def compose(self):
        yield Header()
        self.head = Static("Loading session…", id="head")
        yield self.head
        self.bkt_table = DataTable(cursor_type="row", zebra_stripes=True, id="buckets")
        yield self.bkt_table
        self.turn_table = DataTable(cursor_type="row", zebra_stripes=True, id="turns")
        yield self.turn_table
        yield Footer()

    def on_mount(self):
        self.bkt_table.add_columns("time", "tokens", "spend", "turns")
        self.turn_table.add_columns(*[c[0] for c in self.COLS])
        self.sub_title = short_proj(self.summary.project, 40)
        self.load_session()

    @work(thread=True, exclusive=True)
    def load_session(self):
        s = model.load_session(self.summary.path)
        self.app.call_from_thread(self._populate, s)

    def _populate(self, s):
        self.session = s
        self.all_turns = list(s.turns)
        self.view = list(s.turns)
        self.bkts = s.buckets()
        flag = " (cost est.)" if pricing.is_estimate(s.model) else ""
        self.head.update(
            f"[b]{s.session_id[:8]}[/b] · {s.model or '?'} · {len(s.turns)} turns · "
            f"out {human(s.out)} · peak-ctx [b]{s.ctx_peak:,}[/b] · "
            f"[b]${s.cost:,.2f}[/b]{flag} · {s.tok_per_s:.0f} tok/s (end-to-end) · "
            f"[dim]click a bucket below to filter turns · a=all[/dim]")
        self._fill_buckets()
        self._fill_turns()

    def _fill_buckets(self):
        b = self.bkts
        mt = max((x["tok"] for x in b), default=0)
        mc = max((x["cost"] for x in b), default=0)
        mn = max((x["turns"] for x in b), default=0)
        self.bkt_table.clear()
        for i, x in enumerate(b):
            self.bkt_table.add_row(
                x["label"],
                f"{_bar(x['tok'], mt)} {human(x['tok'])}",
                f"{_bar(x['cost'], mc)} ${x['cost']:.2f}",
                f"{_bar(x['turns'], mn)} {x['turns']}",
                key=str(i))

    def _fill_turns(self):
        self.view.sort(key=self.COLS[self.sort_i][1], reverse=self.sort_rev)
        self.turn_table.clear()
        for i, t in enumerate(self.view):
            fr = "".join(c for c, x in [("C", t.correction), ("S", t.self_correct),
                                        ("E", t.tool_errors >= 2), ("L", t.looped)] if x) or "·"
            sk = ",".join(sorted(x.split(":")[-1] for x in t.skills)) or "-"
            self.turn_table.add_row(
                str(t.index), f"{t.gap:.0f}s", f"{t.duration:.0f}s", human(t.out),
                human(t.ctx), f"${t.cost:,.2f}", f"{t.tok_per_s:.0f}",
                str(len(t.tools)), fr, sk[:30], key=str(i))

    def action_all_turns(self):
        self.filter = None
        self.view = list(self.all_turns)
        self.sub_title = short_proj(self.summary.project, 40)
        self._fill_turns()

    def action_tools(self):
        if not self.session:
            return
        hist = {}
        for t in self.all_turns:
            for c in t.tools:
                hist[c.name] = hist.get(c.name, 0) + 1
        self.app.push_screen(ToolsScreen(self.session.session_id[:8], hist))

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
            self.sub_title = f"{short_proj(self.summary.project, 30)} · {x['label']} ({len(self.view)} turns)"
            self._fill_turns()
        else:
            self.app.push_screen(TurnScreen(self.session, self.view[int(e.row_key.value)]))


# --------------------------------------------------------------------------- #
class TurnScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]

    def __init__(self, session, turn):
        super().__init__()
        self.session = session
        self.turn = turn

    def compose(self):
        t = self.turn
        yield Header()
        fr = [name for name, x in [("user-correction-next", t.correction),
                                   ("self-correction", t.self_correct),
                                   (f"{t.tool_errors} tool-error(s)", t.tool_errors >= 2),
                                   ("tool-loop", t.looped)] if x]
        fr_line = ("[yellow]friction (suspicion, not proof): " + ", ".join(fr)
                   + "[/yellow]") if fr else "[green]no friction flags[/green]"
        prompt = (t.prompt or "").strip() or "(no text prompt)"
        head = (f"[b]Turn {t.index}[/b] · gap {t.gap:.0f}s · dur [b]{t.duration:.0f}s[/b] · "
                f"in {human(t.fresh)} / out [b]{human(t.out)}[/b] tok · ctx {t.ctx:,} · "
                f"[b]${t.cost:,.2f}[/b] · {t.tok_per_s:.0f} tok/s\n"
                f"skills: {', '.join(sorted(t.skills)) or '-'}\n{fr_line}\n"
                f"[dim]exec = tool run · wall = call→next step · Δ = model think + "
                f"idle after (AskUserQuestion exec = you answering)[/dim]\n\n"
                f"[b]prompt[/b]: {prompt[:300]}")
        yield VerticalScroll(Static(head))
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = f"turn {self.turn.index} commands"
        self.table.add_columns("#", "tool", "exec", "wall", "Δ", "summary")
        for i, c in enumerate(self.turn.tools, 1):
            mark = " ✗" if c.is_error else ""
            delta = max(0.0, c.wall - c.dur)
            self.table.add_row(str(i), c.name + mark, f"{c.dur:.0f}s",
                               f"{c.wall:.0f}s", f"{delta:.0f}s", c.summary or "")
        if not self.turn.tools:
            self.table.add_row("-", "(no tool calls)", "", "", "", "")


# --------------------------------------------------------------------------- #
class SkillScreen(Screen):
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

    def __init__(self, root):
        super().__init__()
        self.root = root
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
        self.sub_title = "skill regret — suspicion, not proof"
        self.table.add_columns(*[c[0] for c in self.COLS])
        self.load()

    @work(thread=True, exclusive=True)
    def load(self):
        def prog(n, total):
            if n % 150 == 0 or n == total:
                self.app.call_from_thread(self.status.update,
                                          f"Analyzing… {n}/{total} sessions")
        agg = model.scan_skill_regret(self.root, progress=prog)
        self.app.call_from_thread(self._populate, agg)

    def _populate(self, agg):
        self.rows = list(agg.items())
        self.status.update(
            "[b]turns[/b]=turns the skill ran · [b]tools[/b]=tool calls it triggered · "
            "[b]asks[/b]=times it asked YOU a question · [b]regret%[/b]=turns with "
            "friction [dim](correlation, not proof)[/dim]. Enter a skill to see what "
            "it actually does.")
        self._fill()

    def _fill(self):
        self.rows.sort(key=self.COLS[self.sort_i][1], reverse=self.sort_rev)
        self.table.clear()
        for i, (sk, a) in enumerate(self.rows):
            pct = 100 * a["regret_turns"] / a["fires"] if a["fires"] else 0
            mark = "~" if a["fires"] < 5 else ""   # low sample
            self.table.add_row(
                sk, f"{a['fires']}{mark}", human(a["out"]), str(a["tools"]),
                str(a["asks"]), f"{pct:.0f}%", key=str(i))

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
        ask_note = ""
        if a["asks"]:
            ask_note = (f"\n[yellow]⚠ asked YOU a question {a['asks']} times "
                        f"({asks_per:.1f}/turn) — a skill that interrupts to ask for "
                        f"extra input slows you down, often unnoticed.[/yellow]")
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
        head = (f"[b]{self.skill}[/b]\n"
                f"ran in [b]{a['fires']}[/b] turns · spent [b]{tstr}[/b] "
                f"({secs / fires:.0f}s/turn) · generated [b]{human(a['out'])}[/b] "
                f"output tok · triggered [b]{a['tools']}[/b] tool calls "
                f"({per_turn:.1f}/turn) · friction in {pct:.0f}% of its turns "
                f"[dim](suspicion)[/dim]{inj_line}{ask_note}\n\n"
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
        self.table.add_columns("tool", "calls", "exec", "wall", "% of its tool use")
        hist = self.data["hist"]
        total = sum(h["calls"] for h in hist.values()) or 1
        for name, h in sorted(hist.items(), key=lambda kv: kv[1]["calls"],
                              reverse=True):
            self.table.add_row(name, str(h["calls"]), f"{h['secs']:.0f}s",
                               f"{h.get('wall', 0):.0f}s",
                               f"{100 * h['calls'] / total:.0f}%")
        if not self.data["hist"]:
            self.table.add_row("(no tool calls)", "0", "0s", "0s", "-")


# --------------------------------------------------------------------------- #
class ToolsScreen(Screen):
    """Which tools were called and how often — for a session or a group."""
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.quit", "Quit")]
    COLS = [("tool", lambda r: r[0], False),
            ("calls", lambda r: r[1], True),
            ("share", lambda r: r[1], True)]

    def __init__(self, scope, hist):
        super().__init__()
        self.scope = scope
        self.rows = list(hist.items())
        self.total = sum(hist.values()) or 1
        self.sort_i, self.sort_rev = 1, True

    def compose(self):
        yield Header()
        yield Static(f"[b]Tools — {self.scope}[/b] · [b]{self.total:,}[/b] tool calls "
                     f"across {len(self.rows)} tool types · click a header to sort",
                     id="head")
        self.table = DataTable(cursor_type="row", zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self):
        self.sub_title = "tools called"
        self.table.add_columns("tool", "calls", "% of all calls")
        self._fill()

    def _fill(self):
        self.rows.sort(key=self.COLS[self.sort_i][1], reverse=self.sort_rev)
        self.table.clear()
        m = max((c for _, c in self.rows), default=1)
        for name, c in self.rows:
            self.table.add_row(name, f"{c:,}",
                               f"{_bar(c, m, 8)} {100 * c / self.total:.1f}%")

    def on_data_table_header_selected(self, e):
        i = e.column_index
        self.sort_rev = self.COLS[i][2] if i != self.sort_i else not self.sort_rev
        self.sort_i = i
        self._fill()


# --------------------------------------------------------------------------- #
class ClaudeTraceApp(App):
    CSS = """
    #status { height: auto; color: $text-muted; padding: 0 1; }
    #head { height: auto; padding: 0 1; }
    #buckets { height: 38%; border-bottom: solid $primary; }
    DataTable { height: 1fr; }
    """
    TITLE = "csa"

    def __init__(self, root):
        super().__init__()
        self.root = root

    def on_mount(self):
        self.push_screen(BrowserScreen(self.root))


def run(root):
    ClaudeTraceApp(root).run()
