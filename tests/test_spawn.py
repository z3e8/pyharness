"""spawn(): a spawned child is a full scoped Session running in a parent-side
thread. These tests pin the contract: spawn is approval-gated and returns a
handle immediately; wait() collects distilled reports (single or list, with a
timeout that leaves children running); children run in parallel; the child
holds only its body plus granted capabilities (never spawn — depth one by
construction); it shares the parent's workspace and audit chain; its budget
slice settles into the parent; its approvals bubble to the same human,
labeled and serialized; close() cooperatively stops stragglers; and a failed
child comes back as data, not an exception."""

import threading

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

    def complete(self, *, system, messages, tier="mid", tools=None, max_tokens=None, on_token=None, on_thinking=None, cache_anchor=None):
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


def test_spawn_returns_handle_and_wait_returns_report_envelope(tmp_path):
    llm = ScriptedLLM([_text("REPORT: all done")])
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        handle = parent.broker.call("spawn", "spawn", "do the thing")
        assert handle == "s-spawn-01"  # a plain string — it must cross IPC as JSON
        result = parent.broker.call("spawn", "wait", handle)
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
        handle = parent.broker.call("spawn", "spawn", "write the file", tools=())
        result = parent.broker.call("spawn", "wait", handle)
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
        parent.broker.call("spawn", "wait", parent.broker.call("spawn", "spawn", "t"))
    finally:
        parent.close()
    assert llm.child_budget is not None and llm.child_budget.limit_usd == 2.0


def test_spawn_budget_explicit_capped_by_remaining(tmp_path):
    llm = ScriptedLLM([_text("done")])
    parent = _session(tmp_path, llm, budget=Budget(limit_usd=8.0), approver=lambda req: True)
    try:
        parent.broker.call("spawn", "wait", parent.broker.call("spawn", "spawn", "t", budget_usd=100.0))
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


def test_child_preamble_is_cache_stable_across_budgets(tmp_path):
    # The child preamble must not embed the per-child budget slice or step
    # ceiling: those vary per spawn, so baking them into the cached system
    # prefix would give every sibling a distinct prompt and defeat the prompt
    # cache. Same grant -> byte-identical preamble regardless of walls.
    session = _session(tmp_path, ScriptedLLM([]))
    try:
        granted = frozenset({"web", "http"})
        preamble = session._child_preamble(granted)
        assert session._child_preamble(granted) == preamble
        assert "$" not in preamble  # no dollar budget figure
        assert "steps" not in preamble  # no numeric step ceiling
    finally:
        session.close()


def test_child_approvals_bubble_to_parent_labeled(tmp_path):
    seen = []

    def approver(req):
        seen.append(req.summary)
        return True

    llm = ScriptedLLM([_cell("save_skill('x', 'desc', 'steps')"), _text("done")])
    parent = _session(tmp_path, llm, approver=approver)
    try:
        parent.broker.call("spawn", "wait", parent.broker.call("spawn", "spawn", "save it", tools=("skills",)))
    finally:
        parent.close()
    assert seen[0].startswith("spawn sub-session")
    assert seen[1].startswith("[spawn-01]")


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
        result = parent.broker.call("spawn", "wait", parent.broker.call("spawn", "spawn", "t"))
    finally:
        parent.close()
    assert not result.ok
    assert result.outcome == "error"
    assert "spawn failed" in result.report


# ---- async: handles, parallelism, timeout, status, shutdown ------------------


class GatedLLM:
    """complete() blocks on an event first — a child mid-completion, for tests
    that need a child pinned in the running state."""

    def __init__(self, completions, gate):
        self._completions = list(completions)
        self.gate = gate
        self.child_budget = None

    def complete(self, *, system, messages, tier="mid", tools=None, max_tokens=None,
                 on_token=None, on_thinking=None, cache_anchor=None):
        if not self.gate.wait(5.0):
            raise AssertionError("gate never opened")
        return self._completions.pop(0)

    def with_budget(self, budget):
        self.child_budget = budget
        return self


class BarrierLLM:
    """complete() rendezvouses at a barrier — it only returns if the expected
    number of children are inside a completion at the same time, which is the
    proof of parallelism."""

    def __init__(self, barrier):
        self.barrier = barrier

    def complete(self, *, system, messages, tier="mid", tools=None, max_tokens=None,
                 on_token=None, on_thinking=None, cache_anchor=None):
        self.barrier.wait(timeout=5)
        return _text("done")

    def with_budget(self, budget):
        return self


def test_spawn_is_immediate_and_children_run_in_parallel(tmp_path):
    llm = BarrierLLM(threading.Barrier(2))
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        h1 = parent.broker.call("spawn", "spawn", "left half")
        h2 = parent.broker.call("spawn", "spawn", "right half")
        results = parent.broker.call("spawn", "wait", [h1, h2])
    finally:
        parent.close()
    # Both children were inside a completion simultaneously (else the barrier
    # would have timed out and both would report failure).
    assert [r.ok for r in results] == [True, True]
    assert [r.session for r in results] == ["s-spawn-01", "s-spawn-02"]


def test_wait_timeout_leaves_children_running(tmp_path):
    gate = threading.Event()
    llm = GatedLLM([_text("late report")], gate)
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        handle = parent.broker.call("spawn", "spawn", "slow task")
        with pytest.raises(TimeoutError, match="still running"):
            parent.broker.call("spawn", "wait", handle, timeout=0.05)
        assert parent.broker.call("spawn", "spawn_status") == [
            {"session": handle, "state": "running", "spent_usd": 0.0}
        ]
        gate.set()
        result = parent.broker.call("spawn", "wait", handle)
    finally:
        gate.set()
        parent.close()
    assert result.ok and result.report == "late report"
    assert parent.broker.call("spawn", "spawn_status")[0]["state"] == "done"


def test_wait_none_collects_every_child(tmp_path):
    llm = ScriptedLLM([_text("a"), _text("b")])
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        parent.broker.call("spawn", "spawn", "one")
        parent.broker.call("spawn", "spawn", "two")
        results = parent.broker.call("spawn", "wait")
    finally:
        parent.close()
    assert len(results) == 2 and all(r.ok for r in results)


def test_wait_unknown_handle_rejected(tmp_path):
    parent = _session(tmp_path, ScriptedLLM([]), approver=lambda req: True)
    try:
        with pytest.raises(ValueError, match="unknown spawn handle"):
            parent.broker.call("spawn", "wait", "nope")
    finally:
        parent.close()


def test_shutdown_cancels_running_children_cooperatively(tmp_path):
    # Child step 1 is pinned at the gate; shutdown() drops its budget slice to
    # spent, so once released the loop's next budget check ends the child.
    gate = threading.Event()
    llm = GatedLLM([_cell("x = 1"), _text("never reached")], gate)
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        handle = parent.broker.call("spawn", "spawn", "long task")
        abandoned = parent._spawn_cap.shutdown(join_timeout_s=0.05)
        assert abandoned == [handle]  # still pinned at the gate
        gate.set()
        result = parent.broker.call("spawn", "wait", handle)
        assert not result.ok  # ended by the zeroed budget slice, not an answer
    finally:
        gate.set()
        parent.close()


def test_concurrent_children_keep_the_audit_chain_intact(tmp_path):
    from pyharness.audit import verify_chain

    llm = ScriptedLLM([
        _cell("write('a.txt', 'a')"), _cell("write('b.txt', 'b')"),
        _text("done a"), _text("done b"),
    ])
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        h1 = parent.broker.call("spawn", "spawn", "write a", tools=())
        h2 = parent.broker.call("spawn", "spawn", "write b", tools=())
        parent.broker.call("spawn", "wait", [h1, h2])
    finally:
        parent.close()
    ok, bad = verify_chain(tmp_path / "s" / "audit.jsonl")
    assert ok, f"audit chain broken at line {bad}"
