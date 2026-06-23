# claude-session-analyzer — User Guide

A complete reference for every screen, column, and metric. For the quick pitch
and screen mockups, see the [README](../README.md).

---

## Contents

1. [Install & first run](#install--first-run)
2. [Concepts & vocabulary](#concepts--vocabulary)
3. [The text CLI](#the-text-cli)
4. [The TUI: navigation](#the-tui-navigation)
5. [Screen — Browser](#screen--browser)
6. [Screen — Session](#screen--session)
7. [Screen — Turn detail](#screen--turn-detail)
8. [Screen — Skill regret](#screen--skill-regret)
9. [Screen — Skill detail](#screen--skill-detail)
10. [Screen — Tools](#screen--tools)
11. [Reading the numbers honestly](#reading-the-numbers-honestly)
12. [Worked examples](#worked-examples)
13. [Limits & FAQ](#limits--faq)

---

## Install & first run

```bash
git clone git@github.com:yonk-labs/claude-session-analyzer.git
cd claude-session-analyzer
uv venv .venv && uv pip install --python .venv/bin/python -e .
# or, without uv:  python3 -m pip install -e .
```

The text CLI needs only the standard library. The TUI needs
[Textual](https://textual.textualize.io/) (`>=8`), installed by the command above.

First run:

```bash
csa          # text report over ~/.claude/projects
csa --tui    # interactive browser
```

No configuration. `csa` reads the JSONL transcripts Claude Code already
writes to `~/.claude/projects/<project>/<session-id>.jsonl` (and that session's
`subagents/*.jsonl`). It never writes to them.

---

## Concepts & vocabulary

| Term | Meaning |
|---|---|
| **Transcript** | One session's JSONL file. Every line is a message, tool call, tool result, or metadata, with a millisecond timestamp. |
| **Request** | One API call, identified by `requestId`. A single turn can contain many. Token `usage` is reported per request. |
| **requestId folding** | Multiple assistant lines can share one `requestId`; their `usage` is the *same* call. csa counts it **once** (the naive sum double-counts output). |
| **Turn** | One conversational round: a user prompt and everything until the next user prompt. May span many requests and tool calls. |
| **out / fresh / cache-read / cache-write** | Output tokens generated; fresh input (full price); standing context replayed from cache (~10% price); context written to cache (1.25× for 5-min TTL, 2× for 1-hour). |
| **Bloat ratio** | `cache-read ÷ fresh-input`. How many tokens of standing config replay per token you type. High = a heavy global config. |
| **tok/s** | `output ÷ turn wall-clock`. **End-to-end throughput**, not model decode speed (no first-token data exists). |
| **Friction** | A turn shows friction if you corrected it next, it self-corrected, it hit **≥2** tool errors, or it retried the identical command 3×. **Suspicion, not proof.** |
| **Regret** | Output tokens spent in friction turns, attributed to the skill(s) that ran. A small skill that triggers a big cleanup loop has high regret. |
| **asks** | How many times a skill triggered `AskUserQuestion` — i.e. interrupted you for input. |
| **Context weight** | The token size of the SKILL.md text a skill injects into context each time it runs (estimated chars ÷ 4). |

---

## The text CLI

```
csa [ROOT] [--session FILE] [--tui] [--local] [--top N]
```

| Invocation | What it does |
|---|---|
| `csa` | Corpus profile over `ROOT` (default `~/.claude/projects`): global token totals, bloat ratio, estimated spend, and the top-N sessions by spend. |
| `csa --session FILE` | Per-turn table for one transcript: gap, duration, out, ctx, $, tok/s, tool count, friction flags, skills. |
| `csa --tui` | Launch the interactive TUI (opens on the Projects screen). |
| `csa --local` | Scope to **just the current directory's project** — maps the cwd to its Claude Code project slug. Works for the text report and (with `--tui`) lands straight on that project's sessions. |
| `csa /path` | Use a different transcripts root. |
| `--top N` | How many sessions to list in the profile (default 15). |

The CLI is pipeable and stdlib-only — handy for scripts or a quick check without
opening the TUI. `python3 profile.py …` is a kept alias for the same entry point.

---

## The TUI: navigation

- **Enter** drills down (open a session, a turn, a skill).
- **Esc** goes back one screen (quits from the landing screen).
- **Sort:** press **`1`–`9`** to sort by that column (press the same number again
  to reverse), **or** click a column header with the mouse. The number is the
  column's position, left to right.
- **Mouse or arrow keys** move the row cursor; **Enter** selects.
- Global keys: **`q`** quit. Per-screen keys are shown in the footer and status line.

Loading is asynchronous — the corpus scan (~6s for ~1,700 sessions) and the
skill-regret scan (~8s) run in a background thread with a status line, so the UI
stays responsive.

---

## Screen — Projects

The **landing screen**. Sessions rolled up per project, one row each.

**Columns** (sortable): `project` · `sessions` (count) · `$` (total spend) ·
`out` · `in+cache` · `last used`. Default sort: `$` descending.

**Keys:** `Enter` open that project's sessions · **`a`** all sessions across every
project · **`s`** skill regret (all projects) · **`t`** tools (all projects) · `q`
quit. (`csa --tui --local` skips this screen and lands on the Browser for the
current directory's project.)

---

## Screen — Browser

Sessions list — reached by opening a project on the Projects screen, by pressing
**`a`** there for every session, or directly via `--local`. One row per session.

**Status line** shows total session count and approximate token-value spend.

**Columns** (all sortable; click the header):

| Column | Meaning |
|---|---|
| `$` | Estimated spend for the session (output + fresh + cache, per the price table). |
| `out` | Output tokens generated (compact: `2.9M`, `882k`). |
| `in+cache` | Total input footprint = fresh + cache-read + cache-write. This is how big the context got, summed over requests. |
| `turns` | Number of user prompts (conversational rounds). |
| `wall` | Wall-clock span from first to last activity (includes idle gaps; long-running sessions can span days). |
| `tok/s` | End-to-end throughput (output ÷ active time). |
| `model` | The model the session ran on (`opus-4-8`, `sonnet-4-6`, …). |
| `project` | The project directory the session belongs to. |
| `when` | Timestamp of the last activity. |

**Default sort:** `$` descending — your most expensive sessions first.

**Keys:** `Enter` open session · **`s`** skill regret (scoped to this list) · **`t`**
tools (scoped to this list) · `Esc` back to Projects · `q` quit.

---

## Screen — Session

Opened by selecting a session. A summary header, a **control panel** (default) or
the **time graphs** (toggle with `g`), and the turns table below.

### Header line

`<id8> · <model> · <N> turns · out <tok> · peak-ctx <tok> · $<cost> · <tok/s>`.
**peak-ctx** is the largest single-request context the session ever sent — your
high-water mark for how full the window got.

### Control panel (default top region)

The session at a glance:

- **start / end / elapsed** wall-clock times (real timestamps).
- **turns · tool calls · skill loads · MCP calls · subagents · asked you** — the
  activity profile. "asked you" is `AskUserQuestion` count; "skill loads" is how
  many times a skill was invoked; "MCP calls" counts `mcp__…` tools.
- **friction line** — turns flagged, broken down into corrections,
  self-corrections, error-turns (with total tool errors), and retry-loops.
  Labeled *suspicion, not proof*.
- **skills used** — which skills ran, with a count of turns each.

### Time graphs (press `g`)

Toggles the top region to the bucketed bar charts. Each row is a **real
clock-time bucket** (e.g. `06-18 09:14`) — no more "+120m" offsets. Three metrics
per bucket (**tokens / spend / turns**), each a `█` bar scaled to its max so
**spikes are obvious**. Bucket size auto-scales to ~24 buckets across the session.
**Click a bucket to filter** the turns table to that window; press **`a`** to clear.

### Turns table (always below)

Every turn (or the filtered subset). **Default order is chronological**; sort with
`1`–`9` or a header click.

| Column | Meaning |
|---|---|
| `#` | Turn number (chronological). |
| `gap` | Idle time since the previous turn ended (mostly your thinking/typing time). |
| `dur` | Turn duration: last activity − prompt submitted (time to finish). |
| `out` | Output tokens generated in the turn. |
| `ctx` | Peak context sent during the turn. |
| `$` | Turn cost. |
| `tok/s` | End-to-end throughput for the turn. |
| `tools` | Number of tool calls in the turn. |
| `fric` | Friction flags (see below). `·` = none. |
| `skills` | Skills attributed to the turn. |
| `prompt` | The start of the user prompt that began the turn — so you can see *what the turn was doing* at a glance. |

**Friction flags** (`fric`) — suspicion, not proof:

| Flag | Means |
|---|---|
| `C` | Your **next** prompt pushed back (opened with a rejection, or "that's wrong / try again / revert …"). |
| `S` | The assistant **self-corrected** ("my mistake / I was wrong / let me correct"). |
| `E` | **≥2** tool errors in the turn (one alone is normal — a grep miss, a test that fails by design). |
| `L` | A **retry loop** — the same tool with identical arguments called 3+ times. |

**Keys:** `Enter` open turn detail · **`g`** stats ⇄ time graphs · **`t`** this
session's tools view · **`a`** show all turns (clear filter) · `Esc` back · `q` quit.

---

## Screen — Turn detail

Opened by selecting a turn. The deepest level: what actually happened.

**Header** shows the turn's stats — including **in / out tokens** and total
**duration** — the skills that ran, a friction line (named flags, labeled
"suspicion, not proof"), and the **prompt** text that started it.

**Commands table** — every tool call in the turn, in order:

| Column | Meaning |
|---|---|
| `#` | Order within the turn. |
| `tool` | Tool name. A `✗` marks a tool result that errored. |
| `exec` | **Tool-execution latency** (result timestamp − call timestamp): the actual time the call took — a Bash command's run time, a subagent's run, a server tool's round-trip. For `AskUserQuestion` it's *your* answer time. |
| `wall` | Wall-clock from this call to the next step (exec **plus** whatever happened after it). |
| `Δ` | `wall − exec` — the model's thinking and any idle *after* the tool returned. A big Δ on an instant tool (Write, ToolSearch) is pure think time you'd otherwise never see. |
| `summary` | One-line summary of the call (the command, file path, query, or skill name). |

This is where you see, concretely, what a turn spent its time and tokens on — and
which single command ate the clock.

### Step detail — press Enter on a command

Selecting a command opens its **full step**: a header with the tool name and its
exec/wall/Δ timing, then the **complete input** (the whole Bash command, the full
file text written, the entire prompt handed to a subagent — pretty-printed JSON)
and the **captured result** (capped). This is the bottom of the drill-down: from
projects → sessions → a session → a turn → a single command's exact input and
output. `Esc` returns to the commands list.

---

## Screen — Skill regret

Reached with **`s`** from the browser. A corpus-wide leaderboard of every skill,
built by analyzing each session's turns (~8s, with a progress line).

**Banner** explains the columns and that regret is correlation, not proof.

| Column | Meaning |
|---|---|
| `skill` | The skill (or `(none)` for un-attributed work). |
| `turns` | Turns the skill was attributed to. A `~` suffix marks a low sample (<5). |
| `out` | Output tokens generated across those turns — the **reliable "heaviness"** number. |
| `tools` | Total tool calls the skill triggered. |
| `asks` | Times the skill asked **you** a question (`AskUserQuestion`). High = it interrupts you. |
| `regret%` | Share of the skill's turns that showed friction. Noisy — read it with care. |

**Default sort:** `out` descending. Click any header to re-sort.

**Enter a skill** to open its detail screen.

> Why `out` over `regret%`: friction is a heuristic and fires more on long
> agentic skills (a TDD turn with a failing test looks like "friction" but the
> failing test is the point). Treat `out`, `tools`, and `asks` as the solid
> columns; `regret%` as a flag to investigate, not a verdict.

---

## Screen — Skill detail

Opened by selecting a skill. Answers two questions: **what does this skill load,
and what does it actually do?**

**Header** shows:

- how many turns it ran, **total wall-time spent** (and per-turn average), output
  tokens generated, tool calls (and per-turn rate), friction %.
- **context weight** — `loads ~X KB (~Y tok, est) into context each time it runs`,
  with the number of loads observed. A `(heavy!)` tag appears over ~30k tokens.
  This is the SKILL.md text the skill injects on every invocation — the silent
  cost of *having* the skill loaded for a task.
- an **`⚠ asked YOU a question N times`** line when the skill interrupts you — the
  "this skill is slowing me down by asking for extra stuff" signal.

**Tool histogram** — what the skill *actually triggers*, observed from your traces:

| Column | Meaning |
|---|---|
| `tool` | Tool name. |
| `calls` | How many times this skill triggered it. |
| `exec` | Total tool-execution time in that tool across the skill's turns (sum of result−call latencies). `AskUserQuestion` exec is *you* answering. Use it to spot which tools a skill genuinely spends time *running* (Bash, subagents, server tools) vs. instant ones (Edit, Read). |
| `wall` | Total call→next-step time. `wall − exec` ≈ how much model *thinking* the skill does around that tool — e.g. lots of Bash calls each followed by long reasoning. |
| `% of its tool use` | Share of the skill's total tool calls. |

A skill's advertised behavior and its real behavior can differ. This table is the
real one.

---

## Screen — Tools

Reached with **`t`** — from the **browser** it covers **all sessions**; from
**inside a session** it covers **that session**. A histogram of tool usage.

| Column | Meaning |
|---|---|
| `tool` | Tool name. |
| `calls` | How many times it was called. |
| `% of all calls` | Share of all tool calls, with an inline bar. |

**Default sort:** `calls` descending. Click a header to re-sort. Use it to answer
"what does my Claude Code actually *do* all day" (usually: Bash, Read, Edit).

---

## Reading the numbers honestly

csa measures **tax**, not quality. Keep these in mind:

- **High tokens ≠ bad.** A big session that produced a working feature is fine.
  The signal is *tokens with rework* (friction), or *tokens you didn't choose*
  (a skill loading 130k of reference doc you didn't need).
- **Bloat ratio is footprint, not bill.** Cache-read is ~10% price. A 750× bloat
  ratio means a huge constant context, but it's not 750× the cost — output and
  fresh input dominate spend.
- **tok/s is end-to-end.** It blends your typing time, prompt pre-fill, tool
  execution, and decoding. Low tok/s on a turn with a 400k context often means
  "the model spent a while reading all that config," not "the model is slow."
- **Friction is a hint.** Use it to find turns worth opening, not to indict a
  skill. The detail screens give you the evidence to judge.
- **Cost is estimated** for older/unknown models (flagged `(est.)`), and context
  weight is a chars÷4 estimate. Orderings are reliable; treat absolute figures as
  close, not exact.

---

## Worked examples

**"Which skill is silently bloating my context?"**
Browser → `s` → sort by `out` → open the top skill → read its **context weight**.
If it loads tens of thousands of tokens per run, that's your culprit.

**"Is a skill slowing me down by asking for extra input?"**
Browser → `s` → sort by `asks`. Open a high-`asks` skill: the `⚠ asked YOU N
times (X/turn)` line quantifies the interruption.

**"Where did this session spend its money?"**
Browser → open the session → sort the turns table by `$`. Open the priciest turn
to see exactly which commands and skill drove it.

**"Find the spike."**
Open a session → look at the **tokens** bar row → click the tallest bucket → the
turns table now shows only that window.

**"What does my Claude Code actually do all day?"**
Browser → `t`. The corpus tool histogram. (Spoiler: mostly Bash, Read, Edit.)

---

## Limits & FAQ

**Does it phone home / send my data anywhere?** No. It reads local files and runs
locally. Nothing leaves your machine.

**Why is a session's `wall` time days long?** Wall-clock spans the first to last
timestamp, including idle gaps. Claude Code sessions can be resumed across days.

**Why does `(none)` dominate the skill board?** Most work isn't attributed to a
skill — direct coding. `(none)` is the baseline, not a skill.

**Why is TDD's regret% so high?** Test-driven development writes failing tests on
purpose; a failing test reads as a "tool error" to the heuristic. Known false
positive — that's why regret is labeled suspicion and `out` is the trusted column.

**Can it count exact tokens for injected skill content?** Not offline — the
transcript stores the text, not a token count, so context weight uses chars÷4.
It's accurate enough to rank skills by weight.

**It says cost is `(est.)` — why?** The model isn't in the verified price table
(an older or unknown model). Update `csa/pricing.py` to add rates.

**Where are the transcripts?** `~/.claude/projects/<slugged-cwd>/<session>.jsonl`,
plus `…/<session>/subagents/agent-*.jsonl`. Point csa at any root with
`csa /that/root`.
