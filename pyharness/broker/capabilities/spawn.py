from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, field

from ...security.grants import GrantScope
from ...security.policy import ActionCategory

DEFAULT_TOOLS = ("web", "http")
DEFAULT_MAX_STEPS = 15
DEFAULT_TIER = "mid"

# Sentinel target for the session-wide spawn grant. Spawns have no host to scope
# to, so a grant covers every spawn for the rest of the session — approving the
# fan-out of one report once, not each child. Harness-derived constant (never
# agent- or page-supplied), so GrantScope's exact-match rule is safe.
_SPAWN_GRANT_TARGET = "session"


class SpawnLimitExceeded(Exception):
    """Raised when a session has spawned its total budget of sub-sessions."""


@dataclass(frozen=True)
class SpawnResult:
    """The handoff envelope a spawned sub-session returns to the orchestrator.

    `report` is the child's final message verbatim (its distilled summary —
    large results live in the shared workspace, the report points at them);
    `outcome` uses the shared session vocabulary (`answered`,
    `stopped:max_steps`, `stopped:budget`, `error`, ...); `session` is the
    child's name for `inspect_session()` follow-ups."""

    ok: bool
    report: str
    outcome: str
    session: str
    spent_usd: float
    steps: int


@dataclass
class ChildRun:
    """One running (or finished) spawned child, tracked parent-side. `future`
    resolves to the child's `SpawnResult` — the runner converts every failure
    into a result, so `future.result()` never raises for a failed child.
    `budget` is the child's own slice, exposed for live status and for the
    cooperative cancel in `shutdown` (dropping the limit to spent makes the
    child's next budget check raise, ending it at a step boundary). `abandon`
    is the parent-side force-stop for a child that outlives the shutdown join:
    it closes the child session and settles its spend into the parent exactly
    once, so a straggler can never mutate the parent's budget or audit after
    the parent's session_end snapshot."""

    name: str
    future: Future
    budget: object
    abandon: Callable[[], None] | None = field(default=None, compare=False)


class SpawnCapability:
    """Real sub-agents, asynchronous. `spawn` builds a full scoped child
    session — own kernel, own context, own step and budget walls, a capability
    allowlist, optionally a host scope (`allowed_hosts`) confining its
    web/http/browser reach — starts it in a parent-side thread, and returns a
    handle (the child session's name) immediately. `wait` collects distilled reports;
    `spawn_status` shows what is still running and what it has spent. Depth is
    one by construction: a child's capability set never includes spawn.

    Handles are plain strings because they must round-trip the child->parent
    IPC boundary, which carries JSON only.

    The heavy lifting (building and running the child session) lives on the
    parent `Session` and is injected as `start_child`; this class owns the
    gated surface, the count cap, the handle registry, and the approval
    preview."""

    name = "spawn"

    def __init__(self, start_child, session_cap: int = 16):
        self._start_child = start_child
        self.session_cap = session_cap
        self._spawned = 0
        self._lock = threading.Lock()
        self._children: dict[str, ChildRun] = {}

    def exports(self) -> dict:
        return {
            "spawn": self.spawn,
            "wait": self.wait,
            "spawn_status": self.spawn_status,
        }

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """The approval line shows exactly what the child would be granted —
        task, capability set, host scope, budget slice, step ceiling — because
        approving a spawn is approving that whole plan (OUTWARD: the child can
        reach out with everything it was granted)."""
        task = str(kwargs.get("task") or (args[0] if args else "?"))
        tools = kwargs.get("tools") or (args[1] if len(args) > 1 else DEFAULT_TOOLS)
        budget = kwargs.get("budget_usd")
        steps = kwargs.get("max_steps", DEFAULT_MAX_STEPS)
        hosts = kwargs.get("allowed_hosts") or (args[5] if len(args) > 5 else None)
        brief = " ".join(task.split())
        brief = brief[:117] + "..." if len(brief) > 120 else brief
        budget_part = f"${budget:.2f}" if budget is not None else "default slice"
        # Rendered verbatim (not normalized): preview must never raise before
        # the approval record exists; validation happens on execution.
        host_part = f"; hosts: {', '.join(map(str, hosts))}" if hosts else ""
        return (
            ActionCategory.OUTWARD,
            f"spawn sub-session [tools: {', '.join(tools)}{host_part}; "
            f"budget {budget_part}; ≤{steps} steps] — {brief}",
        )

    def scope(self, op: str, args: tuple, kwargs: dict) -> GrantScope | None:
        """Grant key for a spawn approval: action-class "spawn" on a fixed
        session-wide target, so approving with [a] covers every remaining spawn
        this session (a report's whole fan-out) instead of re-prompting per
        child. Only the gated `spawn` op is grantable; `wait`/`spawn_status`
        aren't gated so never reach here."""
        if op != "spawn":
            return None
        return GrantScope("spawn", _SPAWN_GRANT_TARGET)

    def _reserve(self) -> None:
        with self._lock:
            if self._spawned >= self.session_cap:
                raise SpawnLimitExceeded(
                    f"session spawn cap reached ({self.session_cap})"
                )
            self._spawned += 1

    def spawn(
        self,
        task: str,
        tools=DEFAULT_TOOLS,
        budget_usd: float | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        tier: str = DEFAULT_TIER,
        allowed_hosts=None,
    ) -> str:
        self._reserve()
        child = self._start_child(
            task,
            tools=tuple(tools),
            budget_usd=budget_usd,
            max_steps=max_steps,
            tier=tier,
            allowed_hosts=tuple(allowed_hosts) if allowed_hosts is not None else None,
        )
        with self._lock:
            self._children[child.name] = child
        return child.name

    def _snapshot(self) -> dict[str, ChildRun]:
        """A point-in-time copy of the registry. Every reader iterates a
        snapshot taken under the lock — never the live dict — because spawn()
        inserts from other threads (an in-flight spawn during close() would
        otherwise crash iteration with 'dict changed size')."""
        with self._lock:
            return dict(self._children)

    def _resolve(self, handles) -> tuple[list[ChildRun], bool]:
        """Handles -> tracked children. None = every child spawned so far;
        a single handle returns single=True so wait() can unwrap its result."""
        children = self._snapshot()
        if handles is None:
            return list(children.values()), False
        single = isinstance(handles, str)
        names = [handles] if single else list(handles)
        unknown = [n for n in names if n not in children]
        if unknown:
            raise ValueError(
                f"unknown spawn handle(s) {unknown}; known: {sorted(children)}"
            )
        return [children[n] for n in names], single

    def wait(self, handles=None, timeout: float | None = None):
        """Block until the given spawned children finish and return their
        reports — a single `SpawnResult` for a single handle, else a list in
        handle order. `handles=None` waits for every child spawned so far.
        On `timeout` (seconds) raises TimeoutError; the children keep running
        and their results stay collectible with a later wait()."""
        children, single = self._resolve(handles)
        _, pending = futures_wait([c.future for c in children], timeout=timeout)
        if pending:
            still = [c.name for c in children if c.future in pending]
            raise TimeoutError(
                f"still running after {timeout}s: {still}; "
                "results remain collectible with wait()"
            )
        results = [c.future.result() for c in children]
        return results[0] if single else results

    def spawn_status(self) -> list[dict]:
        """One row per spawned child: handle, state, and spend so far — the
        cheap glance while children run in the background."""
        return [
            {
                "session": c.name,
                "state": "done" if c.future.done() else "running",
                "spent_usd": round(c.budget.spent_usd, 6),
            }
            for c in self._snapshot().values()
        ]

    def shutdown(self, join_timeout_s: float = 10.0) -> list[ChildRun]:
        """Session close: cooperatively stop children still running by dropping
        each one's budget limit to what it already spent — its next budget
        check raises and the child settles as `stopped:budget`. Join briefly
        (a child mid-LLM-call finishes that call first); whatever is still
        running after the timeout is abandoned (daemon threads — they die with
        the process) and returned so the caller can record it and force-settle
        it via `ChildRun.abandon`."""
        running = [c for c in self._snapshot().values() if not c.future.done()]
        for child in running:
            child.budget.limit_usd = child.budget.spent_usd
        futures_wait([c.future for c in running], timeout=join_timeout_s)
        return [c for c in running if not c.future.done()]
