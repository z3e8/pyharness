from __future__ import annotations

import threading
from dataclasses import dataclass

from ...security.policy import ActionCategory

DEFAULT_TOOLS = ("web", "http")
DEFAULT_MAX_STEPS = 15
DEFAULT_TIER = "mid"


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


class SpawnCapability:
    """Real sub-agents. A spawned child is a full scoped session — own kernel,
    own context, own step and budget walls, a capability allowlist — that runs
    one task to completion and hands back a distilled report. Depth is one by
    construction: a child's capability set never includes spawn.

    The heavy lifting (building the child session) lives on the parent
    `Session` and is injected as `spawn_session`; this class owns the gated
    surface, the count cap, and the approval preview."""

    name = "spawn"

    def __init__(self, spawn_session, session_cap: int = 16):
        self._spawn_session = spawn_session
        self.session_cap = session_cap
        self._spawned = 0
        self._lock = threading.Lock()

    def exports(self) -> dict:
        return {"spawn": self.spawn}

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """The approval line shows exactly what the child would be granted —
        task, capability set, budget slice, step ceiling — because approving a
        spawn is approving that whole plan (OUTWARD: the child can reach out
        with everything it was granted)."""
        task = str(kwargs.get("task") or (args[0] if args else "?"))
        tools = kwargs.get("tools") or (args[1] if len(args) > 1 else DEFAULT_TOOLS)
        budget = kwargs.get("budget_usd")
        steps = kwargs.get("max_steps", DEFAULT_MAX_STEPS)
        brief = " ".join(task.split())
        brief = brief[:117] + "..." if len(brief) > 120 else brief
        budget_part = f"${budget:.2f}" if budget is not None else "default slice"
        return (
            ActionCategory.OUTWARD,
            f"spawn sub-session [tools: {', '.join(tools)}; budget {budget_part}; "
            f"≤{steps} steps] — {brief}",
        )

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
    ) -> SpawnResult:
        self._reserve()
        return self._spawn_session(
            task,
            tools=tuple(tools),
            budget_usd=budget_usd,
            max_steps=max_steps,
            tier=tier,
        )
