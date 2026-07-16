"""spawn(): a spawned child is a full scoped Session. These tests pin the
contract: the spawn call is approval-gated; the child holds only its body plus
granted capabilities (never spawn — depth one by construction); it shares the
parent's workspace and audit chain; its budget slice settles into the parent;
its approvals bubble to the same human, labeled; and a failed child comes back
as data, not an exception."""

import pytest

from pyharness import Budget, Session, SpawnResult
from pyharness.broker.dispatch import PermissionDenied
from pyharness.llm.client import Completion, ToolCall


class ScriptedLLM:
    """Replays a fixed list of completions in order — the child agent loop
    consumes them. `with_budget` records the child's budget slice and shares
    the same script, so one instance drives parent and child."""

    def __init__(self, completions):
        self._completions = list(completions)
        self.child_budget = None

    def complete(self, *, system, messages, tier="mid", tools=None, max_tokens=None, on_token=None):
        return self._completions.pop(0)

    def with_budget(self, budget):
        self.child_budget = budget
        return self


def _text(t: str) -> Completion:
    return Completion(text=t, tool_calls=[], content=[{"type": "text", "text": t}])


def _cell(code: str, call_id: str = "c1") -> Completion:
    return Completion(
        text="",
        tool_calls=[ToolCall(id=call_id, name="run_python", input={"code": code})],
        content=[{"type": "tool_use", "id": call_id, "name": "run_python", "input": {"code": code}}],
    )


def _session(tmp_path, llm, **kwargs):
    kwargs.setdefault("skills_dir", tmp_path / "skills")
    return Session(tmp_path / "s", llm=llm, **kwargs)


def test_spawn_returns_report_envelope(tmp_path):
    llm = ScriptedLLM([_text("REPORT: all done")])
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        result = parent.broker.call("spawn", "spawn", "do the thing")
    finally:
        parent.close()
    assert isinstance(result, SpawnResult)
    assert result.ok and result.outcome == "answered"
    assert result.report == "REPORT: all done"
    assert result.session == "s-spawn-01"


def test_spawn_requires_approval(tmp_path):
    parent = _session(tmp_path, ScriptedLLM([]))  # no approver -> deny
    try:
        with pytest.raises(PermissionDenied):
            parent.broker.call("spawn", "spawn", "task")
    finally:
        parent.close()


def test_child_holds_body_plus_granted_never_spawn(tmp_path):
    child = Session(
        tmp_path / "c",
        llm=ScriptedLLM([]),
        capabilities=frozenset({"web"}),
        skills_dir=tmp_path / "skills",
    )
    try:
        names = set(child.broker.op_names())
    finally:
        child.close()
    # Body always; tools implied by an external grant.
    assert {"read", "write", "edit", "search", "llm", "map_llm", "search_tools"} <= names
    # Never spawn (depth one); nothing that wasn't granted.
    assert "spawn" not in names
    assert "bash" not in names
    assert "secrets" not in names
    assert "save_skill" not in names


def test_spawn_shares_workspace_and_parent_audit_chain(tmp_path):
    llm = ScriptedLLM([_cell("write('out.txt', 'hi from child')"), _text("wrote out.txt")])
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        result = parent.broker.call("spawn", "spawn", "write the file", tools=())
    finally:
        parent.close()
    # The child wrote into the parent's workspace — the shared data plane.
    assert (parent.workspace.dir / "out.txt").read_text() == "hi from child"
    assert result.steps == 1
    # The child's side effects landed in the parent's audit chain, and the
    # child session dir carries its own trace for inspect_session/show.
    assert "files.write" in (tmp_path / "s" / "audit.jsonl").read_text()
    assert (tmp_path / "s-spawn-01" / "trace.jsonl").exists()


def test_spawn_budget_slice_defaults_to_quarter_of_remaining(tmp_path):
    llm = ScriptedLLM([_text("done")])
    parent = _session(tmp_path, llm, budget=Budget(limit_usd=8.0), approver=lambda req: True)
    try:
        parent.broker.call("spawn", "spawn", "t")
    finally:
        parent.close()
    assert llm.child_budget is not None and llm.child_budget.limit_usd == 2.0


def test_spawn_budget_explicit_capped_by_remaining(tmp_path):
    llm = ScriptedLLM([_text("done")])
    parent = _session(tmp_path, llm, budget=Budget(limit_usd=8.0), approver=lambda req: True)
    try:
        parent.broker.call("spawn", "spawn", "t", budget_usd=100.0)
    finally:
        parent.close()
    assert llm.child_budget.limit_usd == 8.0


def test_budget_absorb():
    parent = Budget(limit_usd=10.0)
    parent.record("m", 1.0)
    child = Budget()
    child.record("m", 0.5)
    child.record("n", 0.25)
    parent.absorb(child)
    assert parent.spent_usd == 1.75
    assert parent.calls == 3
    assert parent.by_model == {"m": 1.5, "n": 0.25}


def test_child_approvals_bubble_to_parent_labeled(tmp_path):
    seen = []

    def approver(req):
        seen.append(req.summary)
        return True

    llm = ScriptedLLM([_cell("save_skill('x', 'desc', 'steps')"), _text("done")])
    parent = _session(tmp_path, llm, approver=approver)
    try:
        parent.broker.call("spawn", "spawn", "save it", tools=("skills",))
    finally:
        parent.close()
    assert seen[0].startswith("spawn sub-session")
    assert seen[1].startswith("[s-spawn-01]")


def test_unknown_spawn_tool_rejected(tmp_path):
    parent = _session(tmp_path, ScriptedLLM([]), approver=lambda req: True)
    try:
        with pytest.raises(ValueError, match="unknown spawn tools"):
            parent.broker.call("spawn", "spawn", "t", tools=("nonsense",))
    finally:
        parent.close()


def test_failed_child_becomes_data(tmp_path):
    llm = ScriptedLLM([])  # the child's first completion raises -> child errors
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        result = parent.broker.call("spawn", "spawn", "t")
    finally:
        parent.close()
    assert not result.ok
    assert result.outcome == "error"
    assert "spawn failed" in result.report
