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

    def complete(
        self,
        *,
        system,
        messages,
        tier="mid",
        tools=None,
        max_tokens=None,
        on_token=None,
        on_thinking=None,
        on_attempt=None,
        cache_anchor=None,
        total_deadline_s=None,
    ):
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
        content=[
            {
                "type": "tool_use",
                "id": call_id,
                "name": "run_python",
                "input": {"code": code},
            }
        ],
    )


def _session(tmp_path, llm, **kwargs):
    kwargs.setdefault("skills_dir", tmp_path / "skills")
    kwargs.setdefault("unsafe_in_process", True)  # the fast test kernel
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
        unsafe_in_process=True,
    )
    try:
        names = set(child.broker.op_names())
    finally:
        child.close()
    # Body always; tools implied by an external grant.
    assert {
        "read",
        "write",
        "edit",
        "search",
        "llm",
        "map_llm",
        "search_tools",
    } <= names
    # Never spawn (depth one); nothing that wasn't granted.
    assert "spawn" not in names
    assert "bash" not in names
    assert "secrets" not in names
    assert "save_skill" not in names


def test_spawn_shares_workspace_and_parent_audit_chain(tmp_path):
    llm = ScriptedLLM(
        [_cell("write('out.txt', 'hi from child')"), _text("wrote out.txt")]
    )
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
    parent = _session(
        tmp_path, llm, budget=Budget(limit_usd=8.0), approver=lambda req: True
    )
    try:
        parent.broker.call("spawn", "wait", parent.broker.call("spawn", "spawn", "t"))
    finally:
        parent.close()
    assert llm.child_budget is not None and llm.child_budget.limit_usd == 2.0


def test_spawn_budget_explicit_capped_by_remaining(tmp_path):
    llm = ScriptedLLM([_text("done")])
    parent = _session(
        tmp_path, llm, budget=Budget(limit_usd=8.0), approver=lambda req: True
    )
    try:
        parent.broker.call(
            "spawn", "wait", parent.broker.call("spawn", "spawn", "t", budget_usd=100.0)
        )
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
        parent.broker.call(
            "spawn",
            "wait",
            parent.broker.call("spawn", "spawn", "save it", tools=("skills",)),
        )
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


def test_spawn_scope_is_session_wide_and_only_for_spawn():
    from pyharness.broker.capabilities.spawn import SpawnCapability

    cap = SpawnCapability(start_child=lambda *a, **k: None)
    scope = cap.scope("spawn", ("task",), {})
    assert scope is not None
    assert scope.action_class == "spawn" and scope.target == "session"
    # wait/spawn_status aren't gated, so they never mint a grant key.
    assert cap.scope("wait", (), {}) is None
    assert cap.scope("spawn_status", (), {}) is None


def test_spawn_grant_covers_later_spawns_without_reprompting(tmp_path):
    """Approving one spawn with [a] (GRANT) mints a session-wide grant, so the
    rest of a report's fan-out proceeds without prompting the human again."""
    from pyharness.broker.dispatch import ApprovalOutcome

    prompted = []

    def approver(req):
        prompted.append(req.action)
        return ApprovalOutcome.GRANT

    # Two plain-text completions: each child answers in one step and finishes.
    parent = _session(
        tmp_path, ScriptedLLM([_text("a"), _text("b")]), approver=approver
    )
    try:
        parent.broker.call("spawn", "spawn", "one", tools=("web",))
        parent.broker.call("spawn", "spawn", "two", tools=("web",))
    finally:
        parent.close()
    # Only the first spawn reached the human; the grant covered the second.
    assert prompted == ["spawn.spawn"]


def test_failed_child_becomes_data(tmp_path):
    llm = ScriptedLLM([])  # the child's first completion raises -> child errors
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        result = parent.broker.call(
            "spawn", "wait", parent.broker.call("spawn", "spawn", "t")
        )
    finally:
        parent.close()
    assert not result.ok
    assert result.outcome == "error"
    assert "spawn failed" in result.report


# ---- async: handles, parallelism, timeout, status, shutdown ------------------


class GatedLLM:
    """complete() blocks on an event first — a child mid-completion, for tests
    that need a child pinned in the running state.

    `entered` is set on the way in, so a test can wait until the child is
    *provably* inside the completion instead of assuming its thread got there.
    Without that handshake a test racing `spawn()` may act while the child is
    still short of the gate, and a step-boundary check (budget, steps) settles
    it early — the child is no longer pinned and the premise is gone."""

    def __init__(self, completions, gate):
        self._completions = list(completions)
        self.gate = gate
        self.entered = threading.Event()
        self.child_budget = None

    def complete(
        self,
        *,
        system,
        messages,
        tier="mid",
        tools=None,
        max_tokens=None,
        on_token=None,
        on_thinking=None,
        on_attempt=None,
        cache_anchor=None,
        total_deadline_s=None,
    ):
        self.entered.set()
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

    def complete(
        self,
        *,
        system,
        messages,
        tier="mid",
        tools=None,
        max_tokens=None,
        on_token=None,
        on_thinking=None,
        on_attempt=None,
        cache_anchor=None,
        total_deadline_s=None,
    ):
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
        # spawn() returns once the child is registered, which is *before* its
        # thread reaches the completion. Wait for the child to be provably
        # inside the gated call: shutdown() zeroes the slice, so a child still
        # short of step 1's budget check would settle as stopped:budget instead
        # of being pinned, and there would be nothing to abandon.
        assert llm.entered.wait(5.0), "child never reached the gate"
        # No wall-clock guess: a child blocked on an unset gate cannot finish,
        # so any join timeout — including none at all — must report it.
        abandoned = parent._spawn_cap.shutdown(join_timeout_s=0)
        assert [c.name for c in abandoned] == [handle]  # still pinned at the gate
        gate.set()
        result = parent.broker.call("spawn", "wait", handle)
        assert not result.ok  # ended by the zeroed budget slice, not an answer
    finally:
        gate.set()
        parent.close()


def test_children_registry_survives_concurrent_spawn_and_iteration():
    # spawn() inserts into _children from caller threads while spawn_status()/
    # _resolve()/shutdown() iterate it (e.g. close() during an in-flight
    # spawn). All access is lock-guarded via a snapshot, so concurrent
    # mutation must never raise "dictionary changed size during iteration".
    from concurrent.futures import Future

    from pyharness.broker.capabilities.spawn import ChildRun, SpawnCapability

    def start_child(task, **kwargs):
        future: Future = Future()
        future.set_result(None)
        return ChildRun(name=f"c-{task}", future=future, budget=Budget())

    cap = SpawnCapability(start_child, session_cap=10_000)
    errors: list[Exception] = []
    stop = threading.Event()

    def spawner(base: int) -> None:
        try:
            for i in range(300):
                cap.spawn(f"{base}-{i}")
        except Exception as exc:  # noqa: BLE001 — collected for the assertion
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                cap.spawn_status()
                cap._resolve(None)
                cap.shutdown(join_timeout_s=0)
        except Exception as exc:  # noqa: BLE001 — collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=spawner, args=(n,)) for n in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads[:4]:
        t.join(timeout=30)
    stop.set()
    for t in threads[4:]:
        t.join(timeout=30)
    assert errors == []
    assert len(cap.spawn_status()) == 1200


def test_abandoned_child_settles_before_snapshot_and_never_after(tmp_path):
    # A child still running when the parent closes is force-settled *before*
    # the session_end snapshot: its spend-so-far lands in the final numbers,
    # and nothing it does afterwards can touch the parent's budget or append
    # to the audit chain.
    import json

    gate = threading.Event()
    llm = GatedLLM([_text("late report")], gate)
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        handle = parent.broker.call("spawn", "spawn", "slow task")
        run = parent._spawn_cap._children[handle]
        assert llm.entered.wait(5.0)  # the child is provably pinned mid-completion
        llm.child_budget.record("m", 0.5)  # child spend before abandonment
        # close() joins abandoned children for 10s by default — too slow here.
        orig_shutdown = parent._spawn_cap.shutdown
        parent._spawn_cap.shutdown = lambda: orig_shutdown(join_timeout_s=0.05)
        parent.close()  # child pinned at the gate -> abandoned + force-settled

        # Settled into the parent before the snapshot; snapshot carries it.
        assert parent.budget.spent_usd == 0.5
        records = [
            json.loads(line)
            for line in (tmp_path / "s" / "trace.jsonl").read_text().splitlines()
        ]
        assert any(
            r["kind"] == "spawn_abandoned" and r["child"] == handle for r in records
        )
        end = next(r for r in records if r["kind"] == "session_end")
        assert end["spent_usd"] == 0.5

        audit_at_close = (tmp_path / "s" / "audit.jsonl").read_bytes()
        # The abandoned child spends more, then finishes after the snapshot.
        llm.child_budget.record("m", 9.9)
        gate.set()
        run.future.result(timeout=10)
        # No second absorb, no post-snapshot audit appends.
        assert parent.budget.spent_usd == 0.5
        assert (tmp_path / "s" / "audit.jsonl").read_bytes() == audit_at_close
    finally:
        gate.set()
        parent.close()  # idempotent — already closed above


def test_display_callback_is_serialized_across_threads(tmp_path):
    # The parent loop and N child threads all call the display callback; the
    # session wraps it in a lock so a renderer's multi-print sequence (and its
    # cross-call state, e.g. the CLI's open-thinking marker) never interleaves.
    import time as _time

    overlaps = []
    active = [0]

    def renderer(kind: str, text: str) -> None:
        active[0] += 1
        if active[0] > 1:
            overlaps.append(active[0])
        _time.sleep(0.001)
        active[0] -= 1

    session = _session(tmp_path, ScriptedLLM([]), on_event=renderer)
    try:
        emit = session._display_event

        def hammer() -> None:
            for _ in range(50):
                emit("note", "x")

        threads = [threading.Thread(target=hammer) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        session.close()
    assert overlaps == []


def test_concurrent_children_keep_the_audit_chain_intact(tmp_path):
    from pyharness.audit import verify_chain

    llm = ScriptedLLM(
        [
            _cell("write('a.txt', 'a')"),
            _cell("write('b.txt', 'b')"),
            _text("done a"),
            _text("done b"),
        ]
    )
    parent = _session(tmp_path, llm, approver=lambda req: True)
    try:
        h1 = parent.broker.call("spawn", "spawn", "write a", tools=())
        h2 = parent.broker.call("spawn", "spawn", "write b", tools=())
        parent.broker.call("spawn", "wait", [h1, h2])
    finally:
        parent.close()
    ok, bad = verify_chain(tmp_path / "s" / "audit.jsonl")
    assert ok, f"audit chain broken at line {bad}"


# --- Host-scoped children (spawn(allowed_hosts=...)) --------------------------


def test_child_session_is_host_scoped(tmp_path):
    from pyharness.security.egress import EgressBlocked

    child = Session(
        tmp_path / "c",
        llm=ScriptedLLM([]),
        capabilities=frozenset({"web"}),
        allowed_hosts=["Api.GitHub.com"],
        skills_dir=tmp_path / "skills",
        unsafe_in_process=True,
    )
    try:
        # One normalized scope, carried by every egress path the child holds.
        assert child.allowed_hosts == frozenset({"api.github.com"})
        assert child.http.allowed_hosts == frozenset({"api.github.com"})
        assert child.browser.allowed_hosts == frozenset({"api.github.com"})
        # An out-of-scope fetch dies at the egress check — before DNS, before
        # any request leaves the box.
        with pytest.raises(EgressBlocked):
            child.broker.call("web", "fetch", "https://attacker.example.net/")
        # Web search would carry the query to the provider, outside the scope.
        with pytest.raises(EgressBlocked):
            child.broker.call("web", "search", "anything")
    finally:
        child.close()


def test_spawn_allowed_hosts_shown_to_approver_and_passed_to_child(tmp_path):
    llm = ScriptedLLM([_text("done")])
    summaries = []

    def approver(request):
        summaries.append(request.summary)
        return True

    parent = _session(tmp_path, llm, approver=approver)
    try:
        handle = parent.broker.call(
            "spawn",
            "spawn",
            "read the API",
            tools=("web",),
            allowed_hosts=["api.github.com", "PyPI.org"],
        )
        result = parent.broker.call("spawn", "wait", handle)
    finally:
        parent.close()
    assert result.ok
    # The human approved the bound: hosts appear in the spawn approval line.
    assert any("hosts: api.github.com, PyPI.org" in s for s in summaries)


def test_spawn_allowed_hosts_requires_a_network_capability(tmp_path):
    parent = _session(tmp_path, ScriptedLLM([]), approver=lambda req: True)
    try:
        with pytest.raises(ValueError, match="network capability"):
            parent.broker.call(
                "spawn", "spawn", "t", tools=(), allowed_hosts=["example.com"]
            )
    finally:
        parent.close()


def test_child_preamble_names_the_host_scope(tmp_path):
    session = _session(tmp_path, ScriptedLLM([]))
    try:
        granted = frozenset({"web"})
        scoped = session._child_preamble(granted, frozenset({"api.github.com"}))
        assert "api.github.com" in scoped
        assert "web search is unavailable" in scoped
        # Unscoped children keep the preamble free of scope talk.
        assert "scoped to these hosts" not in session._child_preamble(granted)
    finally:
        session.close()


def test_child_preamble_scope_claim_is_honest(tmp_path):
    """The scope text must promise only what `allowed_hosts` actually enforces
    (the web/http/browser and MCP-over-HTTP reach) and must name the granted
    capabilities it does NOT govern — a child holding shell or packages must
    never be told all its requests are host-blocked, because they aren't."""
    session = _session(tmp_path, ScriptedLLM([]))
    hosts = frozenset({"api.github.com"})
    try:
        # A scoped child that also holds shell + packages is told, by name,
        # that those are outside the host scope.
        scoped = session._child_preamble(frozenset({"web", "shell", "packages"}), hosts)
        assert "Not covered by the host scope" in scoped
        assert "shell" in scoped.split("Not covered by the host scope")[1]
        assert "packages" in scoped.split("Not covered by the host scope")[1]
        # The blanket claim the old text made must be gone in every variant.
        assert "anywhere else are blocked" not in scoped
        # A network-only child is not warned about capabilities it doesn't hold,
        # but is told local MCP servers stay outside the scope (tools is always
        # implied by a network grant, so add_mcp_server is always reachable).
        web_only = session._child_preamble(frozenset({"web"}), hosts)
        assert "shell" not in web_only
        assert "packages" not in web_only
        assert "MCP" in web_only
    finally:
        session.close()
