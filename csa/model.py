"""Parse Claude Code JSONL transcripts into an analyzable session model.

The whole tool reads from this. A transcript is already a tree (uuid/parentUuid)
with per-request token usage and skill attribution; we fold it into turns
(user-prompt -> next-user-prompt), price each request, bucket over time, and
flag friction (corrections/errors/loops) per abe's guidance.

Honest-labeling notes (abe multi-model review):
  - tokens/s here is END-TO-END throughput (output / turn wall-clock), NOT decode
    speed. Timestamps are completion-only; there is no first-token signal.
  - friction/regret is CORRELATION (suspicion), not proof a skill caused harm.
  - attributionSkill = which skill *fired*, not which skill is *loaded*; we never
    infer per-skill "passive" context cost from it.
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import pricing

# Claude Code names each project dir by slugging the absolute cwd: every run of
# non-alphanumerics becomes a single "-". e.g. /home/u/my_app -> -home-u-my-app.
_HOME_SLUG = re.sub(r"[^A-Za-z0-9]+", "-", str(Path.home()))


def slugify_path(path):
    """The project-dir slug Claude Code would use for a directory."""
    return re.sub(r"[^A-Za-z0-9]+", "-", os.path.abspath(path))


def pretty_project(project, width=40):
    """Readable project name: collapse the home-dir slug prefix to ~/."""
    p = project.replace(_HOME_SLUG + "-", "~/")
    return p[:width]

# --- friction heuristics (regex over text); cheap, suspicion-only -----------
# Tightened to reduce false positives (abe panel: "correction is overdefined").
# A correction is the user's NEXT prompt OPENING with a rejection, or containing
# a strong rejection phrase. Bare "no/stop/actually/sorry" mid-sentence do NOT
# count — they fire on almost every turn and make the metric meaningless.
_CORRECT_START = re.compile(r"^\W*(no|nope|wrong|stop|ugh|nooo|wtf)\b", re.I)
_CORRECT_PHRASE = re.compile(
    r"(that'?s (wrong|incorrect|not right|not what)|not what i (asked|wanted|meant)|"
    r"does ?n'?t work|did ?n'?t work|try again|undo (that|it)|revert (that|it)|"
    r"you (broke|missed|misunderstood)|that broke|still (broken|failing|wrong))", re.I)
_SELF_CORRECT = re.compile(
    r"(my mistake|my apolog|i apolog|i was wrong|that was wrong|i made (a|an) "
    r"(mistake|error)|let me correct|oops|i mis(read|understood|took)|i incorrectly)",
    re.I)


def _is_correction(text):
    head = (text or "")[:160]
    return bool(_CORRECT_START.match(head) or _CORRECT_PHRASE.search(head))


def _ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _is_user_prompt(r):
    return (r.get("type") == "user" and not r.get("isMeta")
            and isinstance((r.get("message") or {}).get("content"), str))


def _tool_summary(inp, limit=60):
    """One-line summary of a tool_use input dict."""
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "description", "skill", "query", "pattern",
              "file_path", "prompt", "path"):
        if k in inp and isinstance(inp[k], str):
            s = " ".join(inp[k].split())
            return s[:limit]
    return ",".join(sorted(inp.keys()))[:limit]


@dataclass
class ToolCall:
    name: str
    summary: str
    ts: datetime = None
    is_error: bool = False
    dur: float = 0.0          # tool-exec seconds: result_ts - call_ts (else gap fallback)
    wall: float = 0.0         # call_ts -> next step: exec + model-think/idle after
    uid: str = ""             # tool_use id, to match its tool_result


@dataclass
class Turn:
    index: int
    start: datetime
    end: datetime
    prompt: str = ""
    out: int = 0
    ctx: int = 0
    fresh: int = 0
    cost: float = 0.0
    tools: list = field(default_factory=list)        # list[ToolCall]
    skills: set = field(default_factory=set)
    gap: float = 0.0                                  # seconds since prev turn end
    correction: bool = False                          # user pushed back next turn
    self_correct: bool = False                        # assistant walked it back
    tool_errors: int = 0
    looped: bool = False                              # same (tool,args) >=3x
    injects: list = field(default_factory=list)       # (skill_name, chars) it loaded

    @property
    def duration(self):
        return (self.end - self.start).total_seconds() if self.start and self.end else 0.0

    @property
    def tok_per_s(self):
        d = self.duration
        return self.out / d if d > 0 else 0.0

    @property
    def friction(self):
        # tool_errors >= 2: a single failed command is normal (a grep miss, a
        # test that fails by design); two+ in one turn is a real struggle.
        return (self.correction or self.self_correct
                or self.tool_errors >= 2 or self.looped)

    @property
    def asks(self):
        """Times this turn asked the user a question (AskUserQuestion)."""
        return sum(1 for c in self.tools if c.name == "AskUserQuestion")


@dataclass
class Session:
    path: Path
    project: str
    session_id: str
    model: str = ""
    turns: list = field(default_factory=list)

    @property
    def start(self):
        return self.turns[0].start if self.turns else None

    @property
    def end(self):
        return self.turns[-1].end if self.turns else None

    @property
    def wall(self):
        return (self.end - self.start).total_seconds() if self.start and self.end else 0.0

    @property
    def out(self):
        return sum(t.out for t in self.turns)

    @property
    def ctx_peak(self):
        return max((t.ctx for t in self.turns), default=0)

    @property
    def cost(self):
        return sum(t.cost for t in self.turns)

    @property
    def tok_per_s(self):
        active = sum(t.duration for t in self.turns)
        return self.out / active if active > 0 else 0.0

    def buckets(self, seconds=None):
        """Time-bucketed (label, tokens, cost, turns) rows for bar graphs.

        Auto-picks a bucket size targeting ~24 bars unless `seconds` given.
        """
        if not self.turns or not self.start:
            return []
        span = max(self.wall, 1)
        if seconds is None:
            seconds = _nice_bucket(span / 24)
        n = int(span // seconds) + 1
        rows = [{"tok": 0, "cost": 0.0, "turns": 0} for _ in range(n)]
        base = self.start
        for t in self.turns:
            i = min(int((t.start - base).total_seconds() // seconds), n - 1)
            rows[i]["tok"] += t.out
            rows[i]["cost"] += t.cost
            rows[i]["turns"] += 1
        out = []
        for i, row in enumerate(rows):
            mins = int(i * seconds // 60)
            out.append({"label": f"+{mins}m", "lo": i * seconds,
                    "hi": (i + 1) * seconds, **row})
        return out


def _nice_bucket(secs):
    for step in (30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 86400):
        if secs <= step:
            return step
    return 86400


def load_session(path):
    """Parse one transcript file into a Session (lowest-level view)."""
    path = Path(path)
    recs = []
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _ts(r.get("timestamp")):
                recs.append(r)
    recs.sort(key=lambda r: _ts(r["timestamp"]))

    model = ""
    for r in recs:
        m = (r.get("message") or {}).get("model")
        if m:
            model = m
            break

    turns, cur, seen = [], None, set()
    tool_counts = {}
    result_ts = {}         # tool_use_id -> when its result came back (per turn)
    pending_skill = None   # set when a Skill tool fires; claims the next text block
    for r in recs:
        if _is_user_prompt(r):
            if cur:
                _finalize(cur, tool_counts, result_ts)
                turns.append(cur)
            ts = _ts(r["timestamp"])
            cur = Turn(index=len(turns) + 1, start=ts, end=ts,
                       prompt=" ".join((r["message"]["content"] or "").split())[:200])
            tool_counts = {}
            result_ts = {}
            # correction: does THIS prompt push back on the previous turn?
            if turns and _is_correction(r["message"]["content"]):
                turns[-1].correction = True
            continue
        if cur is None:
            continue
        cur.end = _ts(r["timestamp"])
        if r.get("attributionSkill"):
            cur.skills.add(r["attributionSkill"])
        typ = r.get("type")
        if typ == "assistant":
            msg = r.get("message") or {}
            for blk in msg.get("content", []) or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_use":
                    name = blk.get("name", "?")
                    summ = _tool_summary(blk.get("input"))
                    cur.tools.append(ToolCall(name, summ, _ts(r.get("timestamp")),
                                              uid=blk.get("id", "")))
                    # loop = the SAME (tool, input) repeated, i.e. a retry — not
                    # just "used Bash a lot" (that's normal agentic work).
                    k = (name, summ)
                    tool_counts[k] = tool_counts.get(k, 0) + 1
                    if name == "Skill":
                        pending_skill = (blk.get("input") or {}).get("skill")
                elif blk.get("type") == "text" and blk.get("text"):
                    if _SELF_CORRECT.search(blk["text"][:200]):
                        cur.self_correct = True
            u, rid = msg.get("usage"), r.get("requestId")
            if u and rid and rid not in seen:
                seen.add(rid)
                out = u.get("output_tokens", 0)
                fresh = u.get("input_tokens", 0)
                cr = u.get("cache_read_input_tokens", 0)
                cc = u.get("cache_creation") or {}
                c5 = cc.get("ephemeral_5m_input_tokens", 0)
                c1 = cc.get("ephemeral_1h_input_tokens", 0)
                if not (c5 or c1):
                    c5 = u.get("cache_creation_input_tokens", 0)
                cur.out += out
                cur.fresh += fresh
                cur.ctx = max(cur.ctx, fresh + cr + c5 + c1)
                cur.cost += pricing.cost(model, out, fresh, cr, c5, c1)
        elif typ == "user":
            content = (r.get("message") or {}).get("content")
            if isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "tool_result":
                        rid = blk.get("tool_use_id")
                        if rid:
                            result_ts[rid] = _ts(r.get("timestamp"))
                        if blk.get("is_error"):
                            cur.tool_errors += 1
                    elif blk.get("type") == "text" and pending_skill:
                        # the SKILL.md body lands as a text block after the Skill
                        # call (no id link) — attribute it by proximity.
                        cur.injects.append((pending_skill, len(blk.get("text") or "")))
                        pending_skill = None
    if cur:
        _finalize(cur, tool_counts, result_ts)
        turns.append(cur)

    # gaps between turns
    prev_end = None
    for t in turns:
        if prev_end:
            t.gap = (t.start - prev_end).total_seconds()
        prev_end = t.end

    proj, sid = _session_key(path)
    return Session(path=path, project=proj, session_id=sid, model=model, turns=turns)


def _finalize(turn, tool_counts, result_ts):
    turn.looped = any(c >= 3 for c in tool_counts.values())
    # per-command time = tool-execution latency (result_ts - call_ts): the actual
    # time the call took, excluding model thinking before it and idle after it.
    # For AskUserQuestion that latency is you answering — which is honest, since
    # waiting for you IS what the tool does. Fall back to gap-to-next only when a
    # tool has no recorded result (e.g. an interrupted final call).
    for i, c in enumerate(turn.tools):
        nxt = turn.tools[i + 1].ts if i + 1 < len(turn.tools) else turn.end
        c.wall = max(0.0, (nxt - c.ts).total_seconds()) if c.ts and nxt else 0.0
        res = result_ts.get(c.uid)
        c.dur = max(0.0, (res - c.ts).total_seconds()) if res and c.ts else c.wall


def _session_key(path):
    parts = Path(path).parts
    try:
        i = parts.index("projects")
        proj = parts[i + 1]
        rest = parts[i + 2:]
        if len(rest) == 1 and rest[0].endswith(".jsonl"):
            return proj, rest[0][:-6]
        return proj, rest[0]
    except (ValueError, IndexError):
        return "?", path.stem


@dataclass
class SessionSummary:
    """Lightweight per-session totals for the browser/profile (no turn objects)."""
    project: str
    session_id: str
    path: Path
    model: str = ""
    out: int = 0
    fresh: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0
    turns: int = 0
    tmin: datetime = None
    tmax: datetime = None
    files: int = 0
    hist: dict = field(default_factory=dict)   # tool-name -> calls (for tools view)

    @property
    def wall(self):
        return (self.tmax - self.tmin).total_seconds() if self.tmin and self.tmax else 0.0

    @property
    def ctx_in(self):
        return self.fresh + self.cache_read + self.cache_write

    @property
    def tok_per_s(self):
        return self.out / self.wall if self.wall > 0 else 0.0


def _main_transcript(any_path, proj, sid):
    """The main <id>.jsonl path for a session, from any of its files."""
    parts = Path(any_path).parts
    try:
        i = parts.index("projects")
        return Path(*parts[:i + 1]) / proj / f"{sid}.jsonl"
    except ValueError:
        return Path(any_path)


def project_totals(summaries):
    """Roll session summaries up per project. Returns list of dicts sorted by cost."""
    by = {}
    for s in summaries:
        p = by.setdefault(s.project, {"project": s.project, "sessions": 0,
                                      "cost": 0.0, "out": 0, "ctx_in": 0, "tmax": None})
        p["sessions"] += 1
        p["cost"] += s.cost
        p["out"] += s.out
        p["ctx_in"] += s.ctx_in
        if s.tmax and (p["tmax"] is None or s.tmax > p["tmax"]):
            p["tmax"] = s.tmax
    return sorted(by.values(), key=lambda d: d["cost"], reverse=True)


def scan_corpus(root):
    """Stream every transcript under root into per-session summaries.

    Folds a session's subagent files into the same summary. Cheap enough to run
    over thousands of sessions: one pass per file, usage deduped by requestId.
    """
    root = Path(root)
    sessions = {}
    for path in root.rglob("*.jsonl"):
        proj, sid = _session_key(path)
        s = sessions.get((proj, sid))
        if s is None:
            s = SessionSummary(project=proj, session_id=sid,
                               path=_main_transcript(path, proj, sid))
            sessions[(proj, sid)] = s
        s.files += 1
        is_main = path.name == f"{sid}.jsonl"
        seen = set()
        try:
            fh = open(path, "r", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = _ts(r.get("timestamp"))
                if t:
                    s.tmin = t if s.tmin is None or t < s.tmin else s.tmin
                    s.tmax = t if s.tmax is None or t > s.tmax else s.tmax
                typ = r.get("type")
                if typ == "assistant":
                    msg = r.get("message") or {}
                    if not s.model and msg.get("model"):
                        s.model = msg["model"]
                    for blk in msg.get("content", []) or []:
                        if isinstance(blk, dict) and blk.get("type") == "tool_use":
                            nm = blk.get("name", "?")
                            s.hist[nm] = s.hist.get(nm, 0) + 1
                    u, rid = msg.get("usage"), r.get("requestId")
                    if u and rid and rid not in seen:
                        seen.add(rid)
                        out = u.get("output_tokens", 0)
                        fresh = u.get("input_tokens", 0)
                        cr = u.get("cache_read_input_tokens", 0)
                        cc = u.get("cache_creation") or {}
                        c5 = cc.get("ephemeral_5m_input_tokens", 0)
                        c1 = cc.get("ephemeral_1h_input_tokens", 0)
                        if not (c5 or c1):
                            c5 = u.get("cache_creation_input_tokens", 0)
                        s.out += out
                        s.fresh += fresh
                        s.cache_read += cr
                        s.cache_write += c5 + c1
                        s.cost += pricing.cost(msg.get("model") or s.model,
                                               out, fresh, cr, c5, c1)
                elif is_main and _is_user_prompt(r):
                    s.turns += 1
    return list(sessions.values())


def scan_skill_regret(root=None, progress=None, paths=None):
    """Per-skill regret by loading transcripts. Scope with `root` (all projects
    under it) or an explicit `paths` list (e.g. one project's sessions).

    Heavier than scan_corpus (builds turns) but only reads main transcripts.
    `progress(done, total)` is called per file. Returns skill_regret()'s shape.
    """
    if paths is not None:
        mains = [Path(p) for p in paths]
    else:
        mains = list(Path(root).glob("*/*.jsonl"))
    agg = {}
    for n, p in enumerate(mains, 1):
        try:
            s = load_session(p)
        except Exception:
            s = None
        if s:
            for sk, a in skill_regret([s]).items():
                b = agg.setdefault(sk, _skill_zero())
                for k, v in a.items():
                    if k == "hist":
                        for nm, hc in v.items():
                            bh = b["hist"].setdefault(
                                nm, {"calls": 0, "secs": 0.0, "wall": 0.0})
                            bh["calls"] += hc["calls"]
                            bh["secs"] += hc["secs"]
                            bh["wall"] += hc.get("wall", 0.0)
                    else:
                        b[k] += v
        if progress:
            progress(n, len(mains))
    return agg


def _skill_zero():
    return {"fires": 0, "out": 0, "regret_out": 0, "regret_turns": 0,
            "tools": 0, "asks": 0, "secs": 0.0, "hist": {},
            "inject_chars": 0, "injections": 0}


def skill_regret(sessions):
    """Per-skill behavior profile. `fires` = turns the skill was attributed to.

    Keys: fires, out, regret_out, regret_turns, tools (total tool calls),
    asks (AskUserQuestion calls), secs (total wall-clock of its turns),
    hist (tool-name -> {calls, secs} = what it triggers and time spent there).
    Friction/regret is suspicion, not proof.
    """
    agg = {}
    for s in sessions:
        for t in s.turns:
            asks = t.asks
            for sk in (t.skills or {"(none)"}):
                a = agg.setdefault(sk, _skill_zero())
                a["fires"] += 1
                a["out"] += t.out
                a["tools"] += len(t.tools)
                a["asks"] += asks
                a["secs"] += t.duration
                for c in t.tools:
                    h = a["hist"].setdefault(c.name,
                                             {"calls": 0, "secs": 0.0, "wall": 0.0})
                    h["calls"] += 1
                    h["secs"] += c.dur     # exec time
                    h["wall"] += c.wall    # call -> next step
                if t.friction:
                    a["regret_out"] += t.out
                    a["regret_turns"] += 1
            # injected SKILL.md weight attributed to the exact skill that loaded
            for name, chars in t.injects:
                a = agg.setdefault(name, _skill_zero())
                a["inject_chars"] += chars
                a["injections"] += 1
    return agg


def _selfcheck():
    # cost math: 1M output on opus = $25
    assert abs(pricing.cost("claude-opus-4-8", out=1_000_000) - 25.0) < 1e-6
    # cache read is 0.1x input: 1M cache-read on opus = $0.50
    assert abs(pricing.cost("claude-opus-4-8", cache_read=1_000_000) - 0.5) < 1e-6
    assert _is_correction("No, that's wrong")
    assert _is_correction("that doesn't work, try again")
    assert not _is_correction("I do not think there is a problem here")  # tightened
    assert _SELF_CORRECT.search("my mistake, fixing it")
    assert not _SELF_CORRECT.search("let me fix the import")  # too common; dropped
    assert _nice_bucket(7) == 30 and _nice_bucket(500) == 600
    print("model selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
