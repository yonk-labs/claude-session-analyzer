"""Verified Claude token pricing (USD per token).

Base rates per million tokens are from the claude-api reference (cached 2026-06-04).
Cache multipliers are the documented standard: read 0.1x input, write-5m 1.25x,
write-1h 2.0x. Source: prompt-caching reference.

# ponytail: hardcoded table, not a live API call. Refresh rates here when they
# change; see _selfcheck() in model.py for the cost-math check.
"""

# (input $/MTok, output $/MTok)
OPUS = (5.0, 25.0)
OPUS_LEGACY = (15.0, 75.0)   # opus 4.1 / 4.0 / 3
SONNET = (3.0, 15.0)
HAIKU = (1.0, 5.0)
FABLE = (10.0, 50.0)

READ_MULT = 0.1
WRITE_5M_MULT = 1.25
WRITE_1H_MULT = 2.0


def _rates(model):
    """(input, output) $/MTok for a (possibly suffixed) model id."""
    m = (model or "").split("[")[0]  # strip "[1m]" etc.
    if m.startswith(("claude-fable-5", "claude-mythos")):
        return FABLE
    if m.startswith("claude-opus-4-") and m[14:15] in ("5", "6", "7", "8"):
        return OPUS
    if m.startswith(("claude-opus-4-1", "claude-opus-4-0", "claude-3-opus")):
        return OPUS_LEGACY
    if "sonnet" in m:
        return SONNET
    if "haiku" in m:
        return HAIKU
    return OPUS  # sensible default for unknown/blank


def cost(model, out=0, fresh=0, cache_read=0, cache_5m=0, cache_1h=0):
    """Dollar cost of one request's token usage."""
    in_rate, out_rate = _rates(model)
    per = 1e-6
    return per * (
        out * out_rate
        + fresh * in_rate
        + cache_read * in_rate * READ_MULT
        + cache_5m * in_rate * WRITE_5M_MULT
        + cache_1h * in_rate * WRITE_1H_MULT
    )


def is_estimate(model):
    """True when we fell back to a default rate (cost is approximate)."""
    m = (model or "").split("[")[0]
    known = m.startswith((
        "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
        "claude-opus-4-1", "claude-opus-4-0", "claude-3-opus",
        "claude-sonnet", "claude-3-7-sonnet", "claude-3-5-sonnet",
        "claude-haiku", "claude-3-5-haiku", "claude-fable-5", "claude-mythos",
    ))
    return not known
