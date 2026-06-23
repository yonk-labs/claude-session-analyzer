"""Tests for the parser/cost/friction core. Runs under pytest OR standalone:

    python3 tests/test_model.py        # plain asserts, no pytest needed
    pytest tests/

Uses a synthetic transcript so it doesn't depend on real ~/.claude data.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from csa import model, pricing  # noqa: E402

T0 = "2026-06-22T10:00:00.000Z"


def _line(**kw):
    return json.dumps(kw)


def _fixture():
    """Two turns. Turn 1: skill + tool, two assistant lines sharing a requestId
    (must NOT double-count). Turn 2: opens with a correction, has a tool error."""
    rows = [
        _line(type="user", timestamp="2026-06-22T10:00:00.000Z",
              message={"role": "user", "content": "Build the thing"}),
        _line(type="assistant", requestId="r1", attributionSkill="superpowers:brainstorming",
              timestamp="2026-06-22T10:00:01.000Z",
              message={"role": "assistant", "model": "claude-opus-4-8",
                       "content": [{"type": "tool_use", "name": "Bash",
                                    "input": {"command": "ls -la"}}],
                       "usage": {"output_tokens": 100, "input_tokens": 200,
                                 "cache_read_input_tokens": 1000,
                                 "cache_creation": {"ephemeral_5m_input_tokens": 50,
                                                    "ephemeral_1h_input_tokens": 0}}}),
        # same requestId, repeated usage -> deduped (output stays 100, not 200)
        _line(type="assistant", requestId="r1",
              timestamp="2026-06-22T10:00:02.000Z",
              message={"role": "assistant", "model": "claude-opus-4-8",
                       "content": [{"type": "text", "text": "done"}],
                       "usage": {"output_tokens": 100, "input_tokens": 200,
                                 "cache_read_input_tokens": 1000,
                                 "cache_creation": {"ephemeral_5m_input_tokens": 50,
                                                    "ephemeral_1h_input_tokens": 0}}}),
        # turn 2 opens with a correction
        _line(type="user", timestamp="2026-06-22T10:00:10.000Z",
              message={"role": "user", "content": "No, that's wrong, try again"}),
        _line(type="assistant", requestId="r2",
              timestamp="2026-06-22T10:00:11.000Z",
              message={"role": "assistant", "model": "claude-opus-4-8",
                       "content": [{"type": "tool_use", "name": "Bash",
                                    "input": {"command": "make"}}],
                       "usage": {"output_tokens": 30, "input_tokens": 10,
                                 "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 0}}),
        # tool error result
        _line(type="user", timestamp="2026-06-22T10:00:12.000Z",
              message={"role": "user", "content": [{"type": "tool_result",
                                                    "is_error": True, "content": "boom"}]}),
    ]
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                    dir=tempfile.gettempdir())
    f.write("\n".join(rows))
    f.close()
    return f.name


def test_pricing():
    assert abs(pricing.cost("claude-opus-4-8", out=1_000_000) - 25.0) < 1e-6
    assert abs(pricing.cost("claude-opus-4-8", cache_read=1_000_000) - 0.5) < 1e-6   # 0.1x
    assert abs(pricing.cost("claude-opus-4-8", cache_5m=1_000_000) - 6.25) < 1e-6    # 1.25x
    assert abs(pricing.cost("claude-opus-4-8", cache_1h=1_000_000) - 10.0) < 1e-6    # 2x
    assert pricing.is_estimate("some-future-model")
    assert not pricing.is_estimate("claude-opus-4-8")


def test_turns_and_dedup():
    s = model.load_session(_fixture())
    assert s.model == "claude-opus-4-8"
    assert len(s.turns) == 2
    # requestId dedup: turn 1 output is 100, not 200
    assert s.turns[0].out == 100
    assert s.out == 130


def test_friction():
    s = model.load_session(_fixture())
    assert s.turns[0].correction is True   # turn-2 prompt pushed back on turn 1
    assert s.turns[1].tool_errors == 1
    assert s.turns[0].friction


def test_cost_and_buckets():
    s = model.load_session(_fixture())
    # turn 1: 100 out, 200 fresh, 1000 cr, 50 c5  (opus)
    expect = pricing.cost("claude-opus-4-8", 100, 200, 1000, 50, 0)
    assert abs(s.turns[0].cost - expect) < 1e-9
    assert s.cost > 0
    assert s.buckets()  # non-empty


def test_skill_regret():
    s = model.load_session(_fixture())
    agg = model.skill_regret([s])
    assert "superpowers:brainstorming" in agg
    assert agg["superpowers:brainstorming"]["fires"] == 1


def test_buckets_window():
    s = model.load_session(_fixture())
    b = s.buckets()
    assert b and b[0]["lo"] == 0 and b[0]["hi"] > b[0]["lo"]


def test_scan_skill_regret():
    import os
    import shutil
    d = tempfile.mkdtemp()
    proj = os.path.join(d, "projects", "-home-test")
    os.makedirs(proj)
    shutil.copy(_fixture(), os.path.join(proj, "sess1.jsonl"))
    agg = model.scan_skill_regret(os.path.join(d, "projects"))
    assert "superpowers:brainstorming" in agg
    assert agg["superpowers:brainstorming"]["fires"] == 1
    shutil.rmtree(d, ignore_errors=True)


def test_loop_needs_identical_args():
    # three DIFFERENT bash commands in one turn must NOT count as a loop
    import os
    rows = [json.dumps({"type": "user", "timestamp": "2026-06-22T10:00:00.000Z",
                        "message": {"role": "user", "content": "go"}})]
    for i, cmd in enumerate(("ls", "pwd", "whoami")):
        rows.append(json.dumps({
            "type": "assistant", "requestId": f"r{i}",
            "timestamp": f"2026-06-22T10:00:0{i + 1}.000Z",
            "message": {"role": "assistant", "model": "claude-opus-4-8",
                        "content": [{"type": "tool_use", "name": "Bash",
                                     "input": {"command": cmd}}],
                        "usage": {"output_tokens": 5, "input_tokens": 5}}}))
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    f.write("\n".join(rows))
    f.close()
    s = model.load_session(f.name)
    assert s.turns[0].looped is False   # varied args -> not a loop
    os.unlink(f.name)


def test_skill_injection():
    import os
    rows = [
        json.dumps({"type": "user", "timestamp": "2026-06-22T10:00:00.000Z",
                    "message": {"role": "user", "content": "go"}}),
        json.dumps({"type": "assistant", "requestId": "r1",
                    "timestamp": "2026-06-22T10:00:01.000Z",
                    "message": {"role": "assistant", "model": "claude-opus-4-8",
                                "content": [{"type": "tool_use", "name": "Skill",
                                             "input": {"skill": "demo"}}],
                                "usage": {"output_tokens": 5, "input_tokens": 5}}}),
        json.dumps({"type": "user", "timestamp": "2026-06-22T10:00:02.000Z",
                    "message": {"role": "user",
                                "content": [{"type": "text", "text": "X" * 4000}]}}),
    ]
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    f.write("\n".join(rows))
    f.close()
    s = model.load_session(f.name)
    assert s.turns[0].injects == [("demo", 4000)]
    a = model.skill_regret([s])["demo"]
    assert a["inject_chars"] == 4000 and a["injections"] == 1
    os.unlink(f.name)


def test_corpus_tool_hist():
    import os
    import shutil
    d = tempfile.mkdtemp()
    proj = os.path.join(d, "projects", "-p")
    os.makedirs(proj)
    shutil.copy(_fixture(), os.path.join(proj, "s.jsonl"))
    rows = model.scan_corpus(os.path.join(d, "projects"))
    assert rows and rows[0].hist.get("Bash", 0) == 2   # fixture has 2 Bash calls
    shutil.rmtree(d, ignore_errors=True)


def test_tool_timing():
    # ls gets a tool_result at +3 -> exec time 2s (not the 9s gap to `make`).
    # make has NO result -> falls back to gap-to-next (turn end) = 0s.
    import os
    rows = [
        json.dumps({"type": "user", "timestamp": "2026-06-22T10:00:00.000Z",
                    "message": {"role": "user", "content": "go"}}),
        json.dumps({"type": "assistant", "requestId": "r1",
                    "timestamp": "2026-06-22T10:00:01.000Z",
                    "message": {"role": "assistant", "model": "claude-opus-4-8",
                                "content": [{"type": "tool_use", "id": "u1",
                                             "name": "Bash", "input": {"command": "ls"}}],
                                "usage": {"output_tokens": 5, "input_tokens": 5}}}),
        json.dumps({"type": "user", "timestamp": "2026-06-22T10:00:03.000Z",
                    "message": {"role": "user",
                                "content": [{"type": "tool_result", "tool_use_id": "u1",
                                             "content": "ok"}]}}),
        json.dumps({"type": "assistant", "requestId": "r2",
                    "timestamp": "2026-06-22T10:00:10.000Z",
                    "message": {"role": "assistant", "model": "claude-opus-4-8",
                                "content": [{"type": "tool_use", "id": "u2",
                                             "name": "Bash", "input": {"command": "make"}}],
                                "usage": {"output_tokens": 5, "input_tokens": 5}}}),
    ]
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    f.write("\n".join(rows))
    f.close()
    s = model.load_session(f.name)
    t = s.turns[0]
    assert t.tools[0].dur == 2.0          # exec: result(+3) - call(+1)
    assert t.tools[0].wall == 9.0         # wall: call(+1) -> next call(+10)
    assert t.tools[1].dur == 0.0          # no result, last call -> turn end
    assert t.tools[1].wall == 0.0
    a = model.skill_regret([s])["(none)"]
    assert a["secs"] == t.duration
    assert a["hist"]["Bash"] == {"calls": 2, "secs": 2.0, "wall": 9.0}
    os.unlink(f.name)


def test_stats_and_capture():
    s = model.load_session(_fixture())
    st = s.stats()
    assert st["turns"] == 2 and st["tools"] == 2          # 2 Bash calls
    assert st["skill_calls"] == 0 and st["mcp"] == 0
    assert st["error_turns"] == 0                          # 1 error < 2 -> not a turn
    assert st["friction_turns"] == 1                       # turn-1 correction
    assert "superpowers:brainstorming" in st["skills"]
    # buckets carry an absolute clock time
    b = s.buckets()
    assert b[0]["at"] is not None
    # each command captured its full input
    assert s.turns[0].tools[0].input == {"command": "ls -la"}


def test_project_helpers():
    import os
    assert model.slugify_path("/home/u/my_app") == "-home-u-my-app"
    home = os.path.abspath(os.path.expanduser("~"))
    assert model.pretty_project(model.slugify_path(home + "/proj")).startswith("~/")
    s1 = model.SessionSummary(project="p", session_id="a", path=None, cost=1.0, out=10)
    s2 = model.SessionSummary(project="p", session_id="b", path=None, cost=2.0, out=20)
    s3 = model.SessionSummary(project="q", session_id="c", path=None, cost=5.0, out=5)
    pt = model.project_totals([s1, s2, s3])
    assert pt[0]["project"] == "q"               # sorted by cost desc
    by = {d["project"]: d for d in pt}
    assert by["p"]["sessions"] == 2 and by["p"]["cost"] == 3.0 and by["p"]["out"] == 30


def test_scan_skill_regret_paths():
    import os
    import shutil
    d = tempfile.mkdtemp()
    proj = os.path.join(d, "projects", "-p")
    os.makedirs(proj)
    p = os.path.join(proj, "s.jsonl")
    shutil.copy(_fixture(), p)
    agg = model.scan_skill_regret(paths=[p])           # explicit paths, no root
    assert "superpowers:brainstorming" in agg
    shutil.rmtree(d, ignore_errors=True)


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    model._selfcheck()
    print("all tests passed")


if __name__ == "__main__":
    _run()
