"""The session index: derivation from JSONL, incrementality, and read-only query."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from pyharness.obs.index import SCHEMA_HELP, query, update_index
from pyharness.tools.skills import record_use, write_skill


def _write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _make_session(root, name, *, ts=1700000000.0, answer="done", errors=(), skill=None):
    d = root / name
    trace = [
        {"ts": ts, "kind": "session_start", "text": "", "session_id": "abc", "root": str(d)},
        {"ts": ts + 1, "kind": "task", "text": "apply to the job"},
        {"ts": ts + 2, "kind": "llm_call", "text": "thinking...", "model": "claude-sonnet-4-6",
         "tier": "mid", "cost_usd": 0.01, "latency_s": 2.5, "input_tokens": 900, "output_tokens": 80},
        {"ts": ts + 3, "kind": "code", "text": "print('hi')"},
        {"ts": ts + 4, "kind": "output", "text": "hi"},
        {"ts": ts + 4.5, "kind": "action_end", "text": "llm.llm", "ok": False,
         "error": "StreamStalled('silence')", "elapsed_s": 240.0},
    ]
    for err in errors:
        trace.append({"ts": ts + 5, "kind": "error", "text": err})
    if skill:
        trace.append({"ts": ts + 6, "kind": "skill_use", "text": skill,
                      "skill": skill, "outcome": "worked", "note": ""})
    if answer is not None:
        trace.append({"ts": ts + 7, "kind": "answer", "text": answer})
    trace.append({"ts": ts + 8, "kind": "session_end", "text": "", "session_id": "abc",
                  "spent_usd": 0.02, "calls": 2, "by_model": {}})
    _write_jsonl(d / "trace.jsonl", trace)
    _write_jsonl(d / "audit.jsonl", [
        {"ts": ts + 3.5, "action": "shell.bash", "ok": True, "args": "'ls'"},
        {"ts": ts + 3.6, "action": "http.request", "decision": "approve", "approved": False, "ok": False},
    ])
    return d


def test_index_derives_sessions_and_children(tmp_path):
    root = tmp_path / ".sessions"
    _make_session(root, "cli-1", skill="greenhouse")
    db = tmp_path / "index.db"

    counts = update_index(db, [root])
    assert counts == {"roots": 1, "sessions": 1, "indexed": 1}

    (session,) = query(db, "SELECT * FROM sessions")
    assert session["name"] == "cli-1"
    assert session["task"] == "apply to the job"
    assert session["outcome"] == "answered"
    assert session["steps"] == 1
    assert session["failed_actions"] == 1  # the ok=False action_end above
    assert session["llm_calls"] == 1
    assert session["cost_usd"] == pytest.approx(0.02)  # session_end totals win
    assert session["actions"] == 2
    assert session["denials"] == 1
    assert session["project"] == str(tmp_path)  # parent of .sessions

    (call,) = query(db, "SELECT * FROM llm_calls")
    assert call["input_tokens"] == 900 and call["model"] == "claude-sonnet-4-6"
    (cell,) = query(db, "SELECT * FROM cells")
    assert cell["code"] == "print('hi')" and cell["output_chars"] == 2
    (use,) = query(db, "SELECT * FROM skill_uses")
    assert use["skill"] == "greenhouse" and use["outcome"] == "worked"


def test_outcome_classification(tmp_path):
    root = tmp_path / ".sessions"
    _make_session(root, "s-answered", ts=100.0)
    _make_session(root, "s-steps", ts=200.0, answer="(stopped: reached max_steps)")
    _make_session(root, "s-budget", ts=300.0, answer=None,
                  errors=["BudgetExceeded: budget exhausted: spent $5"])
    _make_session(root, "s-error", ts=400.0, answer=None, errors=["RuntimeError: boom"])
    _make_session(root, "s-aborted", ts=500.0, answer=None)
    db = tmp_path / "index.db"
    update_index(db, [root])

    rows = query(db, "SELECT name, outcome FROM sessions ORDER BY name")
    outcomes = {r["name"]: r["outcome"] for r in rows}
    assert outcomes == {
        "s-answered": "answered",
        "s-steps": "stopped:max_steps",
        "s-budget": "stopped:budget",
        "s-error": "error",
        "s-aborted": "aborted",
    }


def test_incremental_skip_and_reindex_on_change(tmp_path):
    root = tmp_path / ".sessions"
    d = _make_session(root, "cli-1")
    db = tmp_path / "index.db"
    assert update_index(db, [root])["indexed"] == 1
    # Unchanged fingerprint: skipped. Roots are remembered, so no need to re-pass.
    assert update_index(db, [])["indexed"] == 0

    time.sleep(0.01)
    with (d / "trace.jsonl").open("a") as f:
        f.write(json.dumps({"ts": 1700000010.0, "kind": "task", "text": "again"}) + "\n")
    assert update_index(db, [])["indexed"] == 1
    (session,) = query(db, "SELECT tasks FROM sessions")
    assert session["tasks"] == 2  # replaced, not duplicated


def test_skill_views_and_journal_snapshot(tmp_path):
    root = tmp_path / ".sessions"
    _make_session(root, "cli-1", ts=100.0, skill="greenhouse")
    _make_session(root, "cli-2", ts=200.0, skill="greenhouse")
    skills = tmp_path / "skills"
    write_skill(skills, "greenhouse", "apply via greenhouse", "steps...")
    record_use(skills / "greenhouse", "worked", "smooth")
    db = tmp_path / "index.db"
    update_index(db, [root], skills_dir=skills)

    (stat,) = query(db, "SELECT * FROM skill_stats")
    assert stat["skill"] == "greenhouse" and stat["uses"] == 2 and stat["success_rate"] == 1.0
    runs = query(db, "SELECT run_n, cost_usd FROM skill_run_costs ORDER BY run_n")
    assert [r["run_n"] for r in runs] == [1, 2]
    (skill,) = query(db, "SELECT * FROM skills")
    assert skill["verified"] == 1 and skill["last_outcome"] == "worked"


def test_query_is_readonly_and_blocks_attach(tmp_path):
    root = tmp_path / ".sessions"
    _make_session(root, "cli-1")
    db = tmp_path / "index.db"
    update_index(db, [root])

    with pytest.raises(sqlite3.OperationalError):
        query(db, "DELETE FROM sessions")
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()
    with pytest.raises(sqlite3.DatabaseError):
        query(db, f"ATTACH DATABASE 'file:{other}?mode=ro' AS x")
    # and a row cap
    assert len(query(db, "SELECT * FROM llm_calls", limit=1)) <= 1


def test_query_missing_db_is_helpful(tmp_path):
    with pytest.raises(FileNotFoundError, match="pyharness-index"):
        query(tmp_path / "nope.db", "SELECT 1")


def test_rebuild_from_scratch(tmp_path):
    root = tmp_path / ".sessions"
    _make_session(root, "cli-1")
    db = tmp_path / "index.db"
    update_index(db, [root])
    counts = update_index(db, [], rebuild=True)
    assert counts["indexed"] == 1  # everything re-derived
    assert query(db, "SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 1


def test_schema_help_names_every_table():
    for table in ("sessions", "llm_calls", "cells", "actions", "skill_uses",
                  "skills", "skill_stats", "skill_run_costs", "error_taxonomy"):
        assert table in SCHEMA_HELP
