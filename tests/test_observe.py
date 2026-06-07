import json

from pyharness.observe import build_timeline, session_dirs
from pyharness.trace import TraceLog


def _write_session(d):
    d.mkdir(parents=True, exist_ok=True)
    trace = TraceLog(d / "trace.jsonl")
    trace.record("task", "do a thing")
    trace.record("code", "print(read('x'))")
    trace.record("output", "contents")
    trace.record("answer", "done")
    trace.record("budget", spent_usd=0.0123, calls=2, by_model={"claude-opus-4-8": 0.0123})
    with (d / "audit.jsonl").open("a") as f:
        f.write(json.dumps({"ts": trace_ts(d) + 0.5, "action": "files.read", "ok": True, "args": "'x'"}) + "\n")
        f.write(json.dumps({"ts": trace_ts(d) + 0.6, "action": "shell.bash", "decision": "deny", "ok": False}) + "\n")


def trace_ts(d):
    # first trace line's ts, so audit entries interleave plausibly
    first = json.loads((d / "trace.jsonl").read_text().splitlines()[0])
    return first["ts"]


def test_session_discovery_single_and_parent(tmp_path):
    # A directory that *is* a session is found directly.
    sess = tmp_path / "cli"
    _write_session(sess)
    assert session_dirs(sess) == {"cli": sess}

    # A parent of sessions lists its children.
    other = tmp_path / "run2"
    _write_session(other)
    found = session_dirs(tmp_path)
    assert set(found) == {"cli", "run2"}


def test_timeline_merges_trace_and_audit_in_order(tmp_path):
    sess = tmp_path / "s"
    _write_session(sess)
    timeline = build_timeline(sess)

    events = timeline["events"]
    # Sorted by ts: task/code/output/answer/budget from trace, two audit calls woven in.
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
    kinds = [e.get("kind") for e in events]
    assert "task" in kinds and "call" in kinds and "budget" in kinds

    summary = timeline["summary"]
    assert summary["tasks"] == 1
    assert summary["calls"] == 2
    assert summary["denied"] == 1
    assert summary["budget"]["spent_usd"] == 0.0123
