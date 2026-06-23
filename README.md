# claude-session-analyzer

**See what Claude Code is *really* doing to your context — and which prompt or
skill is quietly slowing you down.**

Claude Code already writes a full JSONL transcript of every session to
`~/.claude/projects/`. `csa` reads those transcripts and turns them into
tokens, cost, time, and **per-skill behavior** — no wrapper, no instrumentation,
no extra capture step. The data is already there; this just reads it.

> A big global `CLAUDE.md`, dozens of skills, and a pile of MCP servers ride in
> your context on **every** turn. One skill can balloon a single turn by 100k+
> tokens. A "helpful" skill can interrupt you with questions you never asked for.
> You usually can't see any of it. Now you can.

---

## Quickstart

```bash
git clone git@github.com:yonk-labs/claude-session-analyzer.git
cd claude-session-analyzer
uv venv .venv && uv pip install --python .venv/bin/python -e .
# or: python3 -m pip install -e .

csa            # corpus profile: spend, bloat ratio, top sessions
csa --tui      # the interactive browser (main surface)
```

The text CLI is stdlib-only. The TUI's one dependency is
[Textual](https://textual.textualize.io/).

---

## What you get

```
csa                  # one-shot text report over every session
csa --tui            # interactive: projects → sessions → drill down
csa --local          # only the current directory's sessions (the cwd's project)
csa --session FILE   # per-turn breakdown of one transcript
csa /other/projects  # point at a different root
```

`--local` works for both the text report and `--tui` — it maps the current
directory to its Claude Code project and scopes everything to it.

### The CLI report

```
======================================================================
USAGE PROFILE  (1,240 sessions under ~/.claude/projects)
======================================================================
  OUT (generated)    :      42,118,540 tok
  IN  fresh (full $) :      14,002,221 tok
  IN  cache-read     :  10,540,883,002 tok   <- standing context, replayed
  IN  cache-write    :     280,114,907 tok
  BLOAT (read/fresh) :           752.8x
  EST. SPEND         :      ~$8,431.55

TOP 15 SESSIONS BY SPEND
        $       out     in+cache  turns   wall  tok/s  project
   612.40 2,901,033 1,201,557,011    225  7740m    6.3  ~/acme-api
   498.10 2,344,981 1,004,219,540    204  9060m    4.3  ~/web-app
   ...
```

`BLOAT` is the headline: for every token you actually type, ~750 tokens of
standing config (CLAUDE.md + skills + MCP tool defs) replay each turn. It's cheap
per token (cache-read is ~10% price) but it's a huge, constant footprint.

---

## The TUI

It opens on a **Projects** overview — sessions rolled up per project. Drill into
one project to see its sessions, or press `a` for every session across all
projects. (`csa --tui --local` skips straight to the current directory's
sessions.) Sample data below is illustrative.

### 1 · Projects — sessions rolled up per project (landing screen)

```
┌ csa · projects ─────────────────────────────────────────────────────────────┐
│ 52 projects · 1,240 sessions · ~$8,432 · Enter a project · a=all sessions      │
├──────────────────────────┬──────────┬──────────┬───────┬──────────┬───────────┤
│ project                  │ sessions │       $ ▼│  out  │ in+cache │ last used │
├──────────────────────────┼──────────┼──────────┼───────┼──────────┼───────────┤
│ ~/acme-api               │    41    │ $2,788   │ 9.2M  │   3.1B   │ 2026-06-21│
│ ~/web-app                │    33    │ $1,799   │ 6.1M  │   2.0B   │ 2026-06-20│
│ ~/data-pipeline          │    10    │ $1,386   │ 2.4M  │   1.0B   │ 2026-06-18│
└──────────────────────────┴──────────┴──────────┴───────┴──────────┴───────────┘
  Enter open project · a all sessions · s skills · t tools · q quit
```

### 2 · Browser — sessions in a project (or all), sortable

```
┌ csa ───────────────────────────────────────────────────────────────┐
│ 1,240 sessions · ~$8,432 token-value · click a header to sort · s=skill regret│
├─────────┬──────┬──────────┬───────┬──────┬───────┬──────────┬─────────────────┤
│      $ ▼│  out │ in+cache │ turns │ wall │ tok/s │ model    │ project         │
├─────────┼──────┼──────────┼───────┼──────┼───────┼──────────┼─────────────────┤
│ $612.40 │ 2.9M │   1.2B   │  225  │ 129h │  6.3  │ opus-4-8 │ ~/acme-api      │
│ $498.10 │ 2.4M │   1.0B   │  204  │ 151h │  4.3  │ opus-4-8 │ ~/web-app       │
│ $372.30 │ 882k │ 564.7M   │   60  │  43h │  5.7  │ opus-4-8 │ ~/data-pipeline │
│ $187.30 │ 896k │ 285.3M   │  140  │  26h │  9.7  │ sonnet-4 │ ~/web-app       │
└─────────┴──────┴──────────┴───────┴──────┴───────┴──────────┴─────────────────┘
  Enter open · s skills · t tools · q quit
```

### 3 · Session — bucketed bars + sortable turns

The bar table is your spike-finder. **Click a bucket to filter the turns below to
that time window** (`a` clears).

```
┌ acme-api ───────────────────────────────────────────────────────────────────┐
│ 3f9c0e1a · opus-4-8 · 225 turns · out 2.9M · peak-ctx 358,958 · $612.40       │
│ 6.3 tok/s (end-to-end) · click a bucket below to filter turns · a=all         │
├───────┬──────────────────┬─────────────────┬───────────────────┬─────────────┤
│ time  │ tokens           │ spend           │ turns             │             │
├───────┼──────────────────┼─────────────────┼───────────────────┼─────────────┤
│ +0m   │ ████████ 412k    │ ██████ $88.10   │ ██████████ 41     │             │
│ +1d   │ ██ 96k           │ █ $14.20        │ ███ 12            │             │
│ +2d   │ ██████████ 511k  │ ██████████ $140 │ █████ 22          │  ← spike    │
├───────┴──────────────────┴─────────────────┴───────────────────┴─────────────┤
│  # │ gap   │ dur   │   out │     ctx │      $ │ t/s │ tools │ fric │ skills    │
├────┼───────┼───────┼───────┼─────────┼────────┼─────┼───────┼──────┼───────────┤
│  1 │   0s  │ 726s  │ 29.1k │  92,008 │  $2.09 │  40 │   12  │  S·  │ brainstorm│
│  2 │ 599s  │  95s  │  6.1k │  96,894 │  $0.20 │  65 │    0  │   ·  │ -         │
│  3 │ 117s  │1217s  │ 64.2k │ 384,334 │  $9.96 │  53 │   44  │ ·EL  │ claude-api│
└────┴───────┴───────┴───────┴─────────┴────────┴─────┴───────┴──────┴───────────┘
  Enter drill into a turn · t tools · a all turns · Esc back · q quit
```

`fric` flags (suspicion, not proof): **C**=you corrected it next · **S**=it
walked itself back · **E**=2+ tool errors · **L**=retried the same command.

### 4 · Turn detail — the commands

```
┌ turn 3 commands ────────────────────────────────────────────────────────────┐
│ Turn 3 · gap 117s · dur 1217s · in 1.9k / out 64.2k tok · ctx 384,334 · $9.96 │
│ skills: claude-api                                                            │
│ friction (suspicion, not proof): 2 tool-error(s), tool-loop                   │
│ exec = tool run · wall = call→next step · Δ = model think + idle after         │
│                                                                               │
│ prompt: current pricing per million tokens for Opus 4.x, Sonnet 4.x…          │
├────┬─────────────┬──────┬──────┬──────┬─────────────────────────────────────┤
│  # │ tool        │ exec │ wall │  Δ   │ summary                             │
├────┼─────────────┼──────┼──────┼──────┼─────────────────────────────────────┤
│  1 │ Skill       │   3s │  46s │  43s │ claude-api                          │
│  2 │ Bash        │   8s │  40s │  32s │ time python3 profile.py --top 15    │
│  3 │ ToolSearch  │   0s │  29s │  29s │ select:mcp__plugin_abe_abe__debate… │
│  4 │ Write       │   0s │  25s │  25s │ csa/pricing.py                      │
└────┴─────────────┴──────┴──────┴──────┴─────────────────────────────────────┘

The Δ column is the quiet one: `time python3 profile.py` *ran* 8s, but the model
then *thought* 32s before its next move. Instant tools (Write, ToolSearch) with a
big Δ are pure think time you'd never have seen.
```

### 5 · Skill regret — which skill is slowing you down (`s`)

`asks` = how often a skill interrupts **you** for input. `regret%` = share of its
turns with friction (correlation, not proof — `out`/`asks`/`tools` are the
trustworthy columns).

```
┌ skill regret — suspicion, not proof ────────────────────────────────────────┐
│ turns=turns it ran · tools=tool calls it triggered · asks=times it asked YOU  │
│ a question · regret%=turns with friction. Enter a skill to see what it does.  │
├────────────────────────────────────┬───────┬──────┬───────┬──────┬───────────┤
│ skill                              │ turns │ out ▼│ tools │ asks │ regret%   │
├────────────────────────────────────┼───────┼──────┼───────┼──────┼───────────┤
│ (none)                             │  4598 │ 33.4M│ 22334 │  270 │   14%     │
│ writing-plans                      │    59 │ 3.2M │  2010 │   33 │   69%     │
│ subagent-driven-development        │    41 │ 2.2M │  1629 │   13 │   66%     │
│ brainstorming                      │    69 │ 2.1M │  1378 │  145 │   61%     │
│ test-driven-development            │    21 │ 1.7M │  1478 │   16 │   86%     │
└────────────────────────────────────┴───────┴──────┴───────┴──────┴───────────┘
  Enter a skill for its profile · Esc back
```

### 6 · Skill detail — what it loads + what it triggers

Open a skill to see its **context weight** (how many tokens its SKILL.md injects
each run) and the histogram of what it *actually* does in your traces.

```
┌ what this skill really does ────────────────────────────────────────────────┐
│ claude-api                                                                   │
│ ran in 1 turns · spent 20m (1217s/turn) · generated 64.2k output tok ·       │
│ triggered 44 tool calls (44.0/turn) · friction in 100% of its turns          │
│ context weight: loads ~509.2 KB (~130,354 tok, est) each run · 1 load (heavy!)│
│                                                                              │
│ What it actually triggers — calls · exec vs wall                             │
│ (wall−exec = model think after; AskUserQuestion exec = you answering):        │
├─────────────────┬───────┬───────┬───────┬───────────────────────────────────┤
│ tool            │ calls │ exec  │ wall  │ % of its tool use                 │
├─────────────────┼───────┼───────┼───────┼───────────────────────────────────┤
│ Bash            │   26  │   7m  │  22m  │ 59%                               │
│ Edit            │    9  │   0m  │   4m  │ 20%                               │
│ Write           │    5  │   0m  │   2m  │ 11%                               │
│ Read            │    4  │   0m  │   1m  │ 9%                                │
└─────────────────┴───────┴───────┴───────┴───────────────────────────────────┘
```

> This is the payoff: `claude-api` silently loads ~130k tokens of reference doc
> every time it fires. `brainstorming` is lighter (~2.5k) but asks you 2+
> questions per turn. Different costs, both invisible until now.

### 7 · Tools — what got called, how often (`t`)

Corpus-wide from the browser, or for one session from inside it.

```
┌ tools called ───────────────────────────────────────────────────────────────┐
│ Tools — 1,240 sessions · 60,543 tool calls across 35 tool types              │
├───────────────────┬─────────┬───────────────────────────────────────────────┤
│ tool              │ calls ▼ │ % of all calls                                │
├───────────────────┼─────────┼───────────────────────────────────────────────┤
│ Bash              │ 21,884  │ ████████ 36.1%                                │
│ Read              │ 11,302  │ ████ 18.7%                                    │
│ Edit              │  8,140  │ ███ 13.4%                                     │
│ TodoWrite         │  4,071  │ █ 6.7%                                        │
│ AskUserQuestion   │    690  │  1.1%                                         │
└───────────────────┴─────────┴───────────────────────────────────────────────┘
```

**Keys:** `Enter` drill in · `Esc` back · click a header to sort · `s` skills ·
`t` tools · `a` all-turns · `q` quit.

➡ Full per-screen reference: **[docs/USER-GUIDE.md](docs/USER-GUIDE.md)**

---

## What it measures (and how honestly)

`csa` measures **tax** — tokens, cost, time — not answer quality. It's
careful about what the numbers can and can't say:

- **`tok/s`** is end-to-end throughput (`output ÷ turn wall-clock`), **not** decode
  speed. Transcripts only have completion timestamps, so there's no
  time-to-first-token.
- **Friction / regret** (corrections, self-corrections, tool errors, retry loops)
  is *correlation, not proof*. It's labeled that way everywhere it appears.
- **`attributionSkill`** is which skill *fired*, not which is *loaded*. Per-skill
  "passive" context cost is never inferred from it.
- **Context weight** (injected SKILL.md size) is estimated as chars ÷ 4. Good for
  ranking skills by weight; not a billing figure.

---

## How it works

Each transcript line carries a millisecond timestamp, a `uuid`/`parentUuid` tree,
per-request token `usage` (output / input / cache-read / cache-creation, keyed by
`requestId`), and an `attributionSkill` tag. `csa`:

1. folds requests that share a `requestId` into one turn (no double-counting),
2. rolls turns up between user prompts,
3. prices each request with a verified per-model table (5m vs 1h cache split exactly),
4. attributes injected SKILL.md text to the skill that loaded it, by position.

## Pricing

Base rates from Anthropic's pricing reference (Opus $5/$25, Sonnet $3/$15,
Haiku $1/$5, Fable $10/$50 per MTok); cache-read 0.1×, cache-write-5m 1.25×,
cache-write-1h 2× input. Unknown/older models fall back to a default rate and are
flagged `(est.)`. Edit `csa/pricing.py` when rates change.

## Limits

- Completion-only timestamps → no first-token latency.
- Friction is a heuristic (suspicion, not proof); a single tool error is *not*
  flagged (≥2 is).
- Skill-content attribution is by proximity (the SKILL.md body has no id link in
  the transcript); verified accurate on real data.

## License

Apache-2.0 © 2026 yonk-labs
