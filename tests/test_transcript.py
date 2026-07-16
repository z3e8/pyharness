"""Read-side views over a session dir: transcript, outcome, digest, latest."""

from __future__ import annotations

import json
import os

import pytest

from pyharness.transcript import (
    classify_outcome,
    latest_session,
    render_transcript,
    session_digest,
)


def _write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _seed_session(root, name):
    d = root / name
    _write_jsonl(
        d / "trace.jsonl",
        [
            {"ts": 1.0, "kind": "task", "text": "apply to job"},
            {"ts": 2.0, "kind": "llm_call", "text": "fetching", "cost_usd": 0.01,
             "system": "SECRET-PROMPT", "messages": [{"role": "user", "text": "hidden"}]},
            {"ts": 3.0, "kind": "code", "text": "print('x')"},
            {"ts": 4.0, "kind": "output", "text": "x"},
            {"ts": 5.0, "kind": "answer", "text": "done"},
            {"ts": 6.0, "kind": "budget", "spent_usd": 0.05},
        ],
    )
    _write_jsonl(
        d / "audit.jsonl",
        [
            {"ts": 3.5, "action": "files.write", "ok": True},
            {"ts": 3.6, "action": "skills.save_skill", "decision": "deny", "ok": False},
        ],
    )
    return d


# --- render_transcript --------------------------------------------------------


def test_render_transcript_drops_prompt_snapshots(tmp_path):
    d = _seed_session(tmp_path, "s")
    text = render_transcript(d / "trace.jsonl")
    assert "[user task]" in text and "apply to job" in text
    assert "[final answer]" in text and "done" in text
    assert "SECRET-PROMPT" not in text and "hidden" not in text


def test_render_transcript_truncates_entries(tmp_path):
    d = tmp_path / "s"
    _write_jsonl(d / "trace.jsonl", [{"ts": 1.0, "kind": "output", "text": "y" * 10_000}])
    text = render_transcript(d / "trace.jsonl")
    assert len(text) < 6000 and "truncated" in text


def test_render_transcript_missing_trace_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        render_transcript(tmp_path / "trace.jsonl")


# --- classify_outcome ---------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(answer="done", errors=0, tasks=1, stopped_budget=False), "answered"),
        (dict(answer="(stopped: reached max_steps)", errors=0, tasks=1,
              stopped_budget=False), "stopped:max_steps"),
        (dict(answer=None, errors=1, tasks=1, stopped_budget=True), "stopped:budget"),
        (dict(answer=None, errors=1, tasks=1, stopped_budget=False), "error"),
        (dict(answer=None, errors=0, tasks=1, stopped_budget=False), "aborted"),
        (dict(answer=None, errors=0, tasks=0, stopped_budget=False), "empty"),
    ],
)
def test_classify_outcome(kwargs, expected):
    assert classify_outcome(**kwargs) == expected


# --- session_digest -----------------------------------------------------------


def test_session_digest_envelope(tmp_path):
    d = _seed_session(tmp_path, "run-1")
    digest = session_digest(d)
    assert digest["name"] == "run-1"
    assert digest["outcome"] == "answered"
    assert digest["task"] == "apply to job"
    assert digest["answer"] == "done"
    assert (digest["tasks"], digest["steps"], digest["llm_calls"], digest["errors"]) == (1, 1, 1, 0)
    # cumulative budget beats the summed per-call costs
    assert digest["cost_usd"] == 0.05
    assert (digest["actions"], digest["denials"]) == (2, 1)
    assert (digest["started"], digest["ended"]) == (1.0, 6.0)
    assert digest["trace"].endswith("trace.jsonl") and digest["audit"].endswith("audit.jsonl")
    assert digest["workspace"].endswith("workspace")


def test_session_digest_empty_dir(tmp_path):
    digest = session_digest(tmp_path)
    assert digest["outcome"] == "empty"
    assert digest["answer"] is None and digest["task"] is None
    assert digest["cost_usd"] == 0.0 and digest["actions"] == 0


def test_session_digest_error_outcome(tmp_path):
    d = tmp_path / "s"
    _write_jsonl(
        d / "trace.jsonl",
        [
            {"ts": 1.0, "kind": "task", "text": "t"},
            {"ts": 2.0, "kind": "error", "text": "RuntimeError: boom"},
        ],
    )
    assert session_digest(d)["outcome"] == "error"


# --- latest_session -----------------------------------------------------------


def test_latest_session_picks_most_recent(tmp_path):
    old = _seed_session(tmp_path, "a")
    new = _seed_session(tmp_path, "b")
    os.utime(old / "trace.jsonl", (1, 1))
    os.utime(new / "trace.jsonl", (2, 2))
    assert latest_session(tmp_path) == new


def test_latest_session_direct_and_missing(tmp_path):
    d = _seed_session(tmp_path, "a")
    assert latest_session(d) == d
    assert latest_session(tmp_path / "nowhere") is None
