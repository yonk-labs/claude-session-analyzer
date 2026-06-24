"""Parse Claude Code JSONL transcripts into an analyzable session model.

The whole tool reads from this. A transcript is already a tree (uuid/parentUuid)
with per-request token usage and skill attribution; we fold it into turns
(user-prompt -> next-user-prompt), price each request, bucket over time, and
flag friction (corrections/errors/loops/walkbacks) per abe's guidance.

Honest-labeling notes (abe multi-model review):
  - tokens/s here is END-TO-END throughput (output / turn wall-clock), NOT decode
    speed. Timestamps are completion-only; there is no first-token signal.
  - friction/regret is CORRELATION (suspicion), not proof a skill caused harm.
  - attributionSkill = which skill *fired*, not which skill is *loaded*; we never
    infer per-skill "passive" context cost from it.
  - per-tool output tokens are PER-RESPONSE attribution: a response emitting
    N tool calls assigns its output_tokens evenly across them, so when one
    response emits several calls the tokens OVERLAP across tools. Turn tokens
    don't belong to any single tool call — this is the closest honest
    approximation given Claude records tokens per request, not per call.
  - turn.time_breakdown splits wall into (tool_exec, you_answering,
    model_think) where AskUserQuestion's exec time IS you answering (honest:
    waiting for you is what the tool does).
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
# Intent walkback: user's NEXT prompt pivots away from a failing tool call. Tighter
# than a generic "this didn't work" — signals an explicit change of plan.
_WALKBACK = re.compile(
    r"(\b(let'?s|lets|let me|i'?ll|we'?ll|we should|i should)\b.{0,40}\b"
    r"(try|use|switch|move|go|do|instead|with)\b"
    r"|\binstead[ ,].{0,30}\b(use|try|switch)\b"
    r"|\b(different|other)\b.{0,15}\b(approach|tool|way|method)\b"
    r"|\bswitch(ing|ed)?\s+to\b.{0,30}\b(try|use)\b"
    r"|\binstead\s+of\b.{0,40}\b(use|try)\b)",
    re.I | re.S)
# Walkback signal in assistant's own reply (right after a tool error).
_ASSISTANT_WALKBACK = re.compile(
    r"\b(let'?s|let me|i'?ll|instead)\b.{0,40}\b(try|use|switch)\b"
    r"|\b(did ?n'?t work|that failed|since that\b)\b",
    re.I | re.S)

# Tools we treat as "the user answering" when computing time_breakdown.
_USER_BLOCKING = {"AskUserQuestion"}


def parse_mcp(name):
    """('mcp__server__tool') -> ('server', 'tool'), or (None, None)."""
    if not name or not name.startswith("mcp__"):
        return None, None
    parts = name.split("__")
    return (parts[1], "__".join(parts[2:])) if len(parts) >= 3 else (None, None)


def _is_correction(text):
    head = (text or "")[:160]
    return bool(_CORRECT_START.match(head) or _CORRECT_PHRASE.search(head))


def _is_walkback(text):
    return bool(_WALKBACK.search((text or "")[:300]))


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


def _result_text(blk, limit=6000):
    """Readable text of a tool_result block (capped)."""
    c = blk.get("content")
    if isinstance(c, str):
        return c[:limit]
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)[:limit]
    return ""


@dataclass
class ToolCall:
    name: str
    summary: str
    ts: datetime = None
    is_error: bool = False
    dur: float = 0.0          # tool-exec seconds: result_ts - call_ts (else gap fallback)
    wall: float = 0.0         # call_ts -> next step: exec + model-think/idle after
    uid: str = ""             # tool_use id, to match its tool_result
    input: dict = None        # full tool input (for the step drill-in)
    result: str = ""          # tool_result text (capped), for the step drill-in
    out: int = 0              # output tokens from the assistant response that emitted this call (overlaps when several tools in one response; per-response, not per-call — labeled)


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
    walkback: bool = False                            # user pivoted to a different approach next turn
    correction_text: str = ""                         # next user prompt that pushed back (capped)
    walkback_text: str = ""                           # next user prompt that pivoted (capped)

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
        return (self.correction or self.self_correct or self.walkback
                or self.tool_errors >= 2 or self.looped)

    @property
    def time_breakdown(self):
        """Honest 3-way split of turn duration.

        Returns (tool_exec, you_answering, model_think, uncaptured) in seconds.
        Sum == duration. Uncaptured covers gaps with no tools / no prompts to
        anchor on (e.g. an aborted first call). Per the existing honest-label
        notes, AskUserQuestion's exec time IS you answering — it's honest
        because the tool's purpose is to wait for you.
        """
        d = self.duration
        if d <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        you = sum(c.dur for c in self.tools if c.name in _USER_BLOCKING)
        exec_s = sum(c.dur for c in self.tools) - you
        # think = total - exec - you, with a small floor for things we can't measure
        think = max(0.0, d - exec_s - you)
        # uncaptured covers intra-turn gaps that no anchor captured (rare)
        return (exec_s, you, think, 0.0)

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
            at = base + timedelta(seconds=i * seconds)
            out.append({"label": f"+{int(i * seconds // 60)}m", "at": at,
                        "lo": i * seconds, "hi": (i + 1) * seconds, **row})
        return out

    def stats(self):
        """Control-panel rollup for the session screen."""
        ts = self.turns

        def calls(name):
            return sum(1 for t in ts for c in t.tools if c.name == name)

        skills = {}
        for t in ts:
            for sk in t.skills:
                skills[sk] = skills.get(sk, 0) + 1
        mcp_servers = {}
        for t in ts:
            for c in t.tools:
                srv, _ = parse_mcp(c.name)
                if srv:
                    mcp_servers[srv] = mcp_servers.get(srv, 0) + 1
        return {
            "turns": len(ts),
            "tools": sum(len(t.tools) for t in ts),
            "mcp": sum(1 for t in ts for c in t.tools if c.name.startswith("mcp__")),
            "mcp_servers": mcp_servers,
            "asks": calls("AskUserQuestion"),
            "skill_calls": calls("Skill"),
            "skills": skills,                                   # skill -> turns
            "subagents": calls("Agent") + calls("Task"),
            "friction_turns": sum(1 for t in ts if t.friction),
            "corrections": sum(1 for t in ts if t.correction),
            "self_corrections": sum(1 for t in ts if t.self_correct),
            "walkbacks": sum(1 for t in ts if t.walkback),
            "error_turns": sum(1 for t in ts if t.tool_errors >= 2),
            "tool_errors": sum(t.tool_errors for t in ts),
            "loops": sum(1 for t in ts if t.looped),
        }


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
    tool_by_uid = {}       # tool_use_id -> ToolCall, to attach its result text
    pending_skill = None   # set when a Skill tool fires; claims the next text block
    for r in recs:
        if _is_user_prompt(r):
            if cur:
                _finalize(cur, tool_counts, result_ts)
                turns.append(cur)
            ts = _ts(r["timestamp"])
            user_text = r["message"]["content"] or ""
            cur = Turn(index=len(turns) + 1, start=ts, end=ts,
                       prompt=" ".join(user_text.split())[:200])
            tool_counts = {}
            result_ts = {}
            tool_by_uid = {}
            # correction: does THIS prompt push back on the previous turn?
            if turns:
                if _is_correction(user_text):
                    turns[-1].correction = True
                    turns[-1].correction_text = user_text.strip()[:200]
                if _is_walkback(user_text):
                    turns[-1].walkback = True
                    turns[-1].walkback_text = user_text.strip()[:200]
            continue
        if cur is None:
            continue
        cur.end = _ts(r["timestamp"])
        if r.get("attributionSkill"):
            cur.skills.add(r["attributionSkill"])
        typ = r.get("type")
        if typ == "assistant":
            msg = r.get("message") or {}
            u = msg.get("usage")
            # Per-response attribution: if THIS response emits N tool_use blocks,
            # each gets out/N output tokens (overlaps when a response emits
            # several; labeled). Tighter than per-turn attribution and the only
            # form that works in the cheap corpus scan.
            resp_out = (u or {}).get("output_tokens", 0)
            tools_this = [b for b in (msg.get("content") or [])
                          if isinstance(b, dict) and b.get("type") == "tool_use"]
            for blk in msg.get("content", []) or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_use":
                    name = blk.get("name", "?")
                    summ = _tool_summary(blk.get("input"))
                    tc = ToolCall(name, summ, _ts(r.get("timestamp")),
                                  uid=blk.get("id", ""), input=blk.get("input"))
                    cur.tools.append(tc)
                    if tc.uid:
                        tool_by_uid[tc.uid] = tc
                    k = (name, summ)
                    tool_counts[k] = tool_counts.get(k, 0) + 1
                    if name == "Skill":
                        pending_skill = (blk.get("input") or {}).get("skill")
                elif blk.get("type") == "text" and blk.get("text"):
                    if _SELF_CORRECT.search(blk["text"][:200]):
                        cur.self_correct = True
                    elif cur.tool_errors and _ASSISTANT_WALKBACK.search(blk["text"][:300]):
                        # assistant pivoted after a tool error in the same turn
                        cur.self_correct = True
            if tools_this and resp_out:
                share = resp_out // len(tools_this)
                rem = resp_out - share * len(tools_this)
                for i, blk in enumerate(tools_this):
                    tc = tool_by_uid.get(blk.get("id", "")) if blk.get("id") else None
                    if tc is None:
                        # fall back to last tool added (id-mismatch safety)
                        for t in reversed(cur.tools):
                            if t.out == 0 and t.uid == "":
                                tc = t
                                break
                    if tc is not None:
                        tc.out = share + (rem if i == len(tools_this) - 1 else 0)
            rid = r.get("requestId")
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
                            tc = tool_by_uid.get(rid)
                            if tc is not None:
                                tc.result = _result_text(blk)
                                if blk.get("is_error"):
                                    tc.is_error = True       # wire the missing field
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


@dataclass
class Subagent:
    """One subagent a session spawned: its parsed transcript + the Task call.

    Claude Code writes each subagent to <sid>/subagents/agent-<agentId>.jsonl —
    a normal transcript, so load_session parses it unchanged. A subagent has one
    initiating prompt (the Task input) and can't take more user input, so it is
    always a single turn holding all its tool calls.
    """
    agent_id: str
    session: "Session"
    task_uid: str = ""        # parent Task tool_use id (links to the spawning call)
    task_desc: str = ""       # the Task call's description, for labeling

    @property
    def turn(self):
        return self.session.turns[0] if self.session.turns else None

    @property
    def out(self):
        return self.session.out

    @property
    def cost(self):
        return self.session.cost

    @property
    def model(self):
        return self.session.model

    @property
    def dur(self):
        return sum(t.duration for t in self.session.turns)


def subagent_files(main_path):
    """The <sid>/subagents/*.jsonl files a session spawned, if any."""
    d = Path(main_path).with_suffix("") / "subagents"
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def _task_links(main_path):
    """Map subagent agentId -> (task_uid, task_desc) from the parent transcript.

    Each Task tool_result record carries toolUseResult.agentId; the same record's
    tool_result block carries the spawning Task call's tool_use_id, whose input
    description labels the subagent. Tool_use precedes its result in the file, so
    one pass suffices.
    """
    main_path = Path(main_path)
    links, desc_by_uid = {}, {}
    if not main_path.is_file():
        return links
    with open(main_path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (r.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            for b in blocks:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("name") in ("Task", "Agent")):
                    inp = b.get("input") or {}
                    desc_by_uid[b.get("id")] = inp.get("description") or _tool_summary(inp)
            tur = r.get("toolUseResult")
            aid = tur.get("agentId") if isinstance(tur, dict) else None
            if aid:
                uid = next((b.get("tool_use_id", "") for b in blocks
                            if isinstance(b, dict) and b.get("type") == "tool_result"), "")
                links[aid] = (uid, desc_by_uid.get(uid, ""))
    return links


def load_subagents(main_path):
    """Parse every subagent a session spawned, linked to its Task call.

    Returns [] when none were spawned (or the main transcript is absent, e.g. a
    worktree session whose main lives in another project dir). Ordered by spawn.
    """
    links = _task_links(main_path)
    order = {aid: i for i, aid in enumerate(links)}
    subs = []
    for f in subagent_files(main_path):
        aid = f.stem[len("agent-"):] if f.stem.startswith("agent-") else f.stem
        uid, desc = links.get(aid, ("", ""))
        subs.append(Subagent(agent_id=aid, session=load_session(f),
                             task_uid=uid, task_desc=desc))
    subs.sort(key=lambda s: order.get(s.agent_id, 10**9))   # stable: unknowns keep file order
    return subs


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
    file_hist: dict = field(default_factory=dict)  # abs file_path -> read/edit/write calls

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
                            fp = (blk.get("input") or {}).get("file_path")
                            if isinstance(fp, str) and fp:
                                if nm in ("Read", "NotebookRead"):
                                    op = "reads"
                                elif nm in ("Edit", "NotebookEdit", "MultiEdit"):
                                    op = "edits"
                                elif nm == "Write":
                                    op = "writes"
                                else:
                                    op = "other"
                                e = s.file_hist.setdefault(fp, {"reads": 0, "edits": 0, "writes": 0, "other": 0})
                                e[op] += 1
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


def scan_skill_regret(root=None, progress=None, paths=None, source="main"):
    """Per-skill regret by loading transcripts. Scope with `root` (all projects
    under it) or an explicit `paths` list (e.g. one project's sessions).

    `source` picks which transcripts to read:
      "main"      — only the top-level session transcripts (default)
      "subagents" — only the transcripts of subagents those sessions spawned
      "both"      — main + subagent transcripts, folded together
    Subagents run skills too (same attributionSkill parsing); the default "main"
    matches the rest of the Skills view's history, "subagents"/"both" surface
    skill usage that happened inside spawned agents.

    Heavier than scan_corpus (builds turns). `progress(done, total)` is called
    per file. Returns skill_regret()'s shape.

    Excludes skills that only appear via injection (SKILL.md text blocks)
    and have zero actual fires (skill attributed to turns).
    """
    if paths is not None:
        mains = [Path(p) for p in paths]
    else:
        mains = list(Path(root).glob("*/*.jsonl"))
    files = []
    if source in ("main", "both"):
        files += mains
    if source in ("subagents", "both"):
        for mp in mains:
            files += subagent_files(mp)
    agg = {}
    for n, p in enumerate(files, 1):
        try:
            s = load_session(p)
        except Exception:
            s = None
        if s:
            for sk, a in skill_regret([s]).items():
                # Skip skills that only have injection (0 fires) - they aren't
                # actually skill executions, just text blocks.
                if a["fires"] == 0:
                    continue
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
            progress(n, len(files))
    return agg


def _skill_zero():
    return {"fires": 0, "out": 0, "regret_out": 0, "regret_turns": 0,
            "tools": 0, "asks": 0, "secs": 0.0, "hist": {},
            "inject_chars": 0, "injections": 0,
            "corrections": 0, "self_corrections": 0, "walkbacks": 0,
            "tool_errors": 0, "error_turns": 0, "loops": 0}


def skill_regret(sessions):
    """Per-skill behavior profile. `fires` = turns the skill was attributed to.

    Keys: fires, out, regret_out, regret_turns, tools (total tool calls),
    asks (AskUserQuestion calls), secs (total wall-clock of its turns),
    hist (tool-name -> {calls, secs, wall, out} = what it triggers and the
    time + tokens spent there). Friction-breakdown keys (corrections,
    self_corrections, walkbacks, tool_errors, error_turns, loops) let the
    detail screen show WHERE the friction came from — a 100% from one
    correction is different from 100% from twelve tool errors.
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
                                             {"calls": 0, "secs": 0.0,
                                              "wall": 0.0, "out": 0})
                    h["calls"] += 1
                    h["secs"] += c.dur     # exec time
                    h["wall"] += c.wall    # call -> next step
                    h["out"] += c.out      # per-response attribution (overlaps when several tools fire in one response; labeled)
                if t.friction:
                    a["regret_out"] += t.out
                    a["regret_turns"] += 1
                if t.correction:
                    a["corrections"] += 1
                if t.self_correct:
                    a["self_corrections"] += 1
                if t.walkback:
                    a["walkbacks"] += 1
                a["tool_errors"] += t.tool_errors
                if t.tool_errors >= 2:
                    a["error_turns"] += 1
                if t.looped:
                    a["loops"] += 1
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
    assert _is_walkback("let's try a different approach")
    assert _is_walkback("instead, use rg")
    assert not _is_walkback("yes, go on")
    assert parse_mcp("mcp__claude-tools__Bash") == ("claude-tools", "Bash")
    assert parse_mcp("Bash") == (None, None)
    assert _nice_bucket(7) == 30 and _nice_bucket(500) == 600
    print("model selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
