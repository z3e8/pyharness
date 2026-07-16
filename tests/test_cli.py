from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pyharness.broker.dispatch import ApprovalOutcome, PermissionDenied
from pyharness.cli import (
    _EXIT_BY_OUTCOME,
    _headless_approver,
    _reflect_enabled,
    _resolve_root,
    _resolve_session,
    _run_once,
    _trace,
)
from pyharness.llm.client import Completion
from pyharness.transcript import session_digest


class ScriptedLLM:
    """Fake LLM returning canned replies in order; a text-only reply ends the
    agent loop with that answer."""

    def __init__(self, replies=("",)):
        self.replies = list(replies)

    def complete(self, *, system=None, messages, tier="cheap", tools=None,
                 max_tokens=None, on_token=None):
        reply = self.replies.pop(0) if self.replies else ""
        return Completion(reply, [], [{"type": "text", "text": reply}])


# --- the REPL trace renderer --------------------------------------------------


def test_llm_token_streams_inline_without_frame(capsys):
    _trace("llm_token", "hello")
    _trace("llm_token", " world")
    out = capsys.readouterr().out
    # Streamed verbatim, no box frame and no newline between chunks.
    assert out == "hello world"


def test_note_and_llm_call_are_suppressed(capsys):
    # Both carry text already shown via streamed llm_token chunks; rendering them
    # again is the double-render bug this suppression fixes.
    _trace("note", "some preamble")
    _trace("llm_call", "the whole answer")
    assert capsys.readouterr().out == ""


def test_code_and_output_still_boxed(capsys):
    _trace("code", "print(1)")
    _trace("output", "1")
    out = capsys.readouterr().out
    assert "┌─ python ─" in out and "print(1)" in out
    assert "┌─ output ─" in out and "1" in out


def test_llm_start_is_suppressed(capsys):
    _trace("llm_start", "")
    assert capsys.readouterr().out == ""


# --- root resolution -----------------------------------------------------------


def test_resolve_root_prefers_cli_arg():
    # An explicit path wins over everything, even a configured persistent workspace.
    root = _resolve_root("runs/foo", {"PYHARNESS_WORKSPACE": "~/ws"}, "ts")
    assert root == Path("runs/foo")


def test_resolve_root_uses_persistent_workspace_env():
    # A stable workspace so dropped/created files survive across runs; ~ expands.
    root = _resolve_root(None, {"PYHARNESS_WORKSPACE": "~/agent-home"}, "ts")
    assert root == Path("~/agent-home").expanduser()


def test_resolve_root_defaults_to_timestamped_session():
    root = _resolve_root(None, {}, "20260101-000000")
    assert root == Path(".sessions/cli-20260101-000000")


def test_reflection_is_opt_in():
    assert not _reflect_enabled({})  # off by default
    assert not _reflect_enabled({"PYHARNESS_REFLECT": "false"})
    assert not _reflect_enabled({"PYHARNESS_REFLECT": "nonsense"})
    assert _reflect_enabled({"PYHARNESS_REFLECT": "true"})
    assert _reflect_enabled({"PYHARNESS_REFLECT": " 1 "})


# --- headless one-shot runs ----------------------------------------------------


def test_headless_approver_denies_by_default_and_never_grants():
    assert _headless_approver(False)(request=None) is ApprovalOutcome.DENY
    # --approve-all answers each request once; a standing grant needs a human.
    assert _headless_approver(True)(request=None) is ApprovalOutcome.ONCE


def _run_kwargs(tmp_path):
    """Hermetic Session overrides: no real index DB, skills dir, or MCP config."""
    return dict(
        out_of_process=False,
        skills_dir=tmp_path / "skills",
        index_db=tmp_path / "index.db",
        mcp_config=None,
    )


def test_run_once_returns_answered_digest(tmp_path):
    digest = _run_once(
        "say hi", tmp_path / "sess",
        budget_usd=1.0, approve_all=False, reflect=False, watch=False,
        llm=ScriptedLLM(["all done"]), **_run_kwargs(tmp_path),
    )
    assert digest["outcome"] == "answered"
    assert digest["answer"] == "all done"
    assert digest["tasks"] == 1
    assert Path(digest["trace"]).exists()
    assert digest["audit"].endswith("audit.jsonl")  # created on first audited call


def test_headless_run_denies_gated_actions(tmp_path):
    from pyharness.cli import _build_session

    session = _build_session(
        tmp_path / "sess", budget_usd=1.0, approver=_headless_approver(False),
        llm=ScriptedLLM(), **_run_kwargs(tmp_path),
    )
    try:
        with pytest.raises(PermissionDenied):
            session.broker.call(
                "skills", "save_skill", name="probe", description="d", instructions="i"
            )
    finally:
        session.close()
    assert session_digest(tmp_path / "sess")["denials"] == 1


def test_exit_codes_cover_every_outcome():
    from pyharness.transcript import classify_outcome  # the outcome vocabulary

    outcomes = {
        classify_outcome(answer=a, errors=e, tasks=t, stopped_budget=b)
        for a in ("x", "(stopped: reached max_steps)", None)
        for e in (0, 1) for t in (0, 1) for b in (False, True)
    }
    assert outcomes == set(_EXIT_BY_OUTCOME)
    assert _EXIT_BY_OUTCOME["answered"] == 0


def test_run_json_prints_one_envelope(monkeypatch, tmp_path, capsys):
    import pyharness.cli as cli

    digest = {"name": "run-1", "outcome": "answered", "answer": "hi",
              "cost_usd": 0.01, "llm_calls": 1}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "_run_once", lambda *a, **k: digest)
    monkeypatch.setattr(sys, "argv", ["pyharness", "run", "say hi", "--json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert json.loads(out) == digest  # exactly one machine-clean JSON object


def test_run_exit_code_reflects_outcome(monkeypatch, tmp_path, capsys):
    import pyharness.cli as cli

    digest = {"name": "run-1", "outcome": "stopped:budget", "answer": None,
              "cost_usd": 1.0, "llm_calls": 3}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "_run_once", lambda *a, **k: digest)
    monkeypatch.setattr(sys, "argv", ["pyharness", "run", "t"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3
    assert "no answer" in capsys.readouterr().out


# --- show ----------------------------------------------------------------------


def _seed_session(root, name):
    d = root / name
    d.mkdir(parents=True)
    entries = [
        {"ts": 1.0, "kind": "task", "text": "probe the thing"},
        {"ts": 2.0, "kind": "llm_call", "text": "on it", "cost_usd": 0.01,
         "system": "SECRET-PROMPT", "messages": []},
        {"ts": 3.0, "kind": "answer", "text": "done"},
    ]
    with (d / "trace.jsonl").open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return d


def test_resolve_session_by_dir_name_and_latest(tmp_path):
    root = tmp_path / ".sessions"
    d = _seed_session(root, "run-1")
    assert _resolve_session(str(d), Path("elsewhere")) == d  # explicit dir path
    assert _resolve_session("run-1", root) == d  # name under root
    assert _resolve_session(None, root) == d  # latest


def test_resolve_session_unknown_exits_with_hint(tmp_path):
    with pytest.raises(SystemExit, match="pyharness-index"):
        _resolve_session("nope", tmp_path)
    with pytest.raises(SystemExit, match="no sessions"):
        _resolve_session(None, tmp_path)


def test_show_digest_json_and_transcript(monkeypatch, tmp_path, capsys):
    import pyharness.cli as cli

    root = tmp_path / ".sessions"
    _seed_session(root, "run-1")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(sys, "argv", ["pyharness", "show"])
    cli.main()
    out = capsys.readouterr().out
    assert "outcome: answered" in out and "name: run-1" in out

    monkeypatch.setattr(sys, "argv", ["pyharness", "show", "--json"])
    cli.main()
    digest = json.loads(capsys.readouterr().out)
    assert digest["outcome"] == "answered" and digest["answer"] == "done"

    monkeypatch.setattr(sys, "argv", ["pyharness", "show", "run-1", "--transcript"])
    cli.main()
    out = capsys.readouterr().out
    assert "[user task]" in out and "probe the thing" in out
    assert "SECRET-PROMPT" not in out  # prompt snapshots never ride along
