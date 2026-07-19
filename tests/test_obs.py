"""The observability builtins: stats, inspect_session, and the history preamble."""

from __future__ import annotations

import json

import pytest

from pyharness.broker.capabilities.obs import ObservabilityCapability
from pyharness.core.session import Session
from pyharness.obs.index import update_index
from pyharness.llm.client import Completion


class RecordingLLM:
    """Fake LLM that captures the inspect_session worker call."""

    def __init__(self, reply="the selector changed"):
        self.reply = reply
        self.calls: list[dict] = []

    def complete(self, *, system=None, messages, tier="cheap", tools=None,
                 max_tokens=None, on_token=None, on_thinking=None, on_attempt=None,
                 cache_anchor=None, total_deadline_s=None):
        self.calls.append({"system": system, "messages": messages, "tier": tier})
        return Completion(self.reply, [], [{"type": "text", "text": self.reply}])


def _seed_session(root, name, *, task="apply to job", answer="done"):
    d = root / name
    d.mkdir(parents=True)
    entries = [
        {"ts": 1.0, "kind": "task", "text": task},
        {"ts": 2.0, "kind": "llm_call", "text": "I will fetch the page",
         "model": "m", "tier": "mid", "cost_usd": 0.01, "latency_s": 1.0,
         "system": "SECRET-PROMPT", "messages": [{"role": "user", "text": "hidden"}]},
        {"ts": 3.0, "kind": "code", "text": "print('x')"},
        {"ts": 4.0, "kind": "output", "text": "x"},
        {"ts": 5.0, "kind": "skill_use", "text": "greenhouse", "skill": "greenhouse",
         "outcome": "failed", "note": "selector gone"},
        {"ts": 6.0, "kind": "answer", "text": answer},
    ]
    with (d / "trace.jsonl").open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return d


def test_stats_returns_schema_without_sql(tmp_path):
    cap = ObservabilityCapability(tmp_path / "index.db", RecordingLLM())
    assert "skill_stats" in cap.stats()


def test_stats_queries_index(tmp_path):
    root = tmp_path / ".sessions"
    _seed_session(root, "cli-1")
    db = tmp_path / "index.db"
    update_index(db, [root])
    cap = ObservabilityCapability(db, RecordingLLM())
    rows = cap.stats("SELECT name, outcome FROM sessions")
    assert rows == [{"name": "cli-1", "outcome": "answered"}]


def test_stats_without_index_configured_is_helpful():
    cap = ObservabilityCapability(None, RecordingLLM())
    with pytest.raises(RuntimeError, match="index"):
        cap.stats("SELECT 1")


def test_inspect_session_hands_transcript_to_worker(tmp_path):
    root = tmp_path / ".sessions"
    _seed_session(root, "cli-1")
    db = tmp_path / "index.db"
    update_index(db, [root])
    llm = RecordingLLM()
    cap = ObservabilityCapability(db, llm)

    answer = cap.inspect_session("cli-1", "why did the skill fail?")
    assert answer == "the selector changed"
    (call,) = llm.calls
    assert call["tier"] == "cheap"
    prompt = call["messages"][0]["content"]
    assert "apply to job" in prompt and "selector gone" in prompt
    assert "why did the skill fail?" in prompt
    # the per-call prompt snapshots must not ride along
    assert "SECRET-PROMPT" not in prompt

    with pytest.raises(KeyError, match="no session"):
        cap.inspect_session("nope", "?")


def test_session_preamble_and_builtins(tmp_path):
    """A session with an index gets stats/inspect_session builtins and an
    ambient history preamble; without one, the builtins exist but are dataless."""
    root = tmp_path / ".sessions"
    _seed_session(root, "cli-0")
    db = tmp_path / "index.db"
    update_index(db, [root])

    s = Session(root / "cli-new", llm=RecordingLLM(), index_db=db,
                skills_dir=tmp_path / "skills")
    try:
        ns = s.broker.namespace()
        assert "stats" in ns and "inspect_session" in ns
        preamble = s.agent.preamble_extra
        assert "Recent sessions" in preamble and "cli-0" in preamble
        # index refresh on open folded cli-0 in; the new session isn't answered yet
        rows = ns["stats"]("SELECT COUNT(*) AS n FROM sessions")
        assert rows[0]["n"] >= 1
    finally:
        s.close()

    bare = Session(tmp_path / "bare", llm=RecordingLLM(), skills_dir=tmp_path / "skills")
    try:
        assert bare.agent.preamble_extra == ""
        with pytest.raises(RuntimeError):
            bare.broker.namespace()["stats"]("SELECT 1")
    finally:
        bare.close()


def test_close_folds_session_into_index(tmp_path):
    db = tmp_path / "index.db"
    root = tmp_path / ".sessions"
    s = Session(root / "cli-a", llm=RecordingLLM(), index_db=db,
                skills_dir=tmp_path / "skills")
    s.close()
    from pyharness.obs.index import query

    rows = query(db, "SELECT name, outcome FROM sessions")
    assert rows == [{"name": "cli-a", "outcome": "empty"}]
