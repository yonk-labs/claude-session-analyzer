"""claude-trace text CLI: understand your Claude Code usage from transcripts.

  claude-trace                 corpus profile (all sessions under the root)
  claude-trace --session FILE  per-turn breakdown for one transcript
  claude-trace --tui           launch the interactive browser (see tui.py)

The TUI is the main surface; this CLI is the pipeable/scriptable view and the
fast path for "what's my usage" without launching a UI.
"""
import argparse
from pathlib import Path

from . import model, pricing

DEFAULT_ROOT = Path.home() / ".claude" / "projects"


def _short(project, width=34):
    return project.replace("-home-yonk-", "~/")[:width]


def profile(root, top):
    rows = model.scan_corpus(root)
    g_out = sum(s.out for s in rows)
    g_fresh = sum(s.fresh for s in rows)
    g_cr = sum(s.cache_read for s in rows)
    g_cw = sum(s.cache_write for s in rows)
    g_cost = sum(s.cost for s in rows)
    est = any(pricing.is_estimate(s.model) for s in rows)

    print("=" * 74)
    print(f"USAGE PROFILE  ({len(rows)} sessions under {root})")
    print("=" * 74)
    print(f"  OUT (generated)    : {g_out:>15,} tok")
    print(f"  IN  fresh (full $) : {g_fresh:>15,} tok")
    print(f"  IN  cache-read     : {g_cr:>15,} tok   <- standing context, replayed")
    print(f"  IN  cache-write    : {g_cw:>15,} tok")
    ratio = g_cr / g_fresh if g_fresh else 0
    print(f"  BLOAT (read/fresh) : {ratio:>15.1f}x")
    print(f"  EST. SPEND         : {'~$' + format(g_cost, ',.2f'):>15}"
          + ("   (some models default-priced)" if est else ""))
    print()

    rows.sort(key=lambda s: s.cost, reverse=True)
    print(f"TOP {top} SESSIONS BY SPEND")
    print(f"  {'$':>9} {'out':>9} {'in+cache':>12} {'turns':>6} {'wall':>6} {'tok/s':>6}  project")
    for s in rows[:top]:
        print(f"  {s.cost:>8.2f} {s.out:>9,} {s.ctx_in:>12,} {s.turns:>6} "
              f"{s.wall/60:>5.0f}m {s.tok_per_s:>6.1f}  {_short(s.project)}")
    print()
    print("NOTE: cost is dominated by output + fresh input; cache-read is ~0.1x,")
    print("      so a huge bloat ratio inflates context, not necessarily spend.")


def session(path):
    s = model.load_session(path)
    flag = " (cost est.)" if pricing.is_estimate(s.model) else ""
    print(f"SESSION {Path(path).name}")
    print(f"  model={s.model or '?'}  turns={len(s.turns)}  out={s.out:,}  "
          f"peak-ctx={s.ctx_peak:,}  ${s.cost:.2f}{flag}  tok/s={s.tok_per_s:.1f}")
    print(f"  {'#':>3} {'gap':>6} {'dur':>6} {'out':>8} {'ctx':>9} "
          f"{'$':>7} {'t/s':>5} {'tools':>5} {'fr':>3}  skills")
    for t in s.turns:
        fr = "".join(c for c, b in [("C", t.correction), ("S", t.self_correct),
                                    ("E", t.tool_errors >= 2), ("L", t.looped)] if b) or "-"
        sk = ",".join(sorted(x.split(":")[-1] for x in t.skills)) or "-"
        print(f"  {t.index:>3} {t.gap:>5.0f}s {t.duration:>5.0f}s {t.out:>8,} "
              f"{t.ctx:>9,} {t.cost:>7.2f} {t.tok_per_s:>5.0f} {len(t.tools):>5} "
              f"{fr:>3}  {sk[:34]}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claude-trace",
                                 description="understand your Claude Code usage")
    ap.add_argument("root", nargs="?", default=str(DEFAULT_ROOT))
    ap.add_argument("--session", help="per-turn detail for one transcript file")
    ap.add_argument("--tui", action="store_true", help="launch interactive browser")
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args(argv)
    if a.tui:
        from .tui import run
        run(a.root)
    elif a.session:
        session(a.session)
    else:
        profile(a.root, a.top)


if __name__ == "__main__":
    main()
