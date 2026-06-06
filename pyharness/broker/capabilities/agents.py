from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

WORKER_SYSTEM = (
    "You are a focused worker sub-agent. Complete the single task you are given "
    "and return only the result — no preamble, no meta-commentary."
)


class SubAgentLimitExceeded(Exception):
    """Raised when a session has spawned its total budget of sub-agents."""


@dataclass(frozen=True)
class Result:
    """One sub-agent outcome. Fan-out returns these so a failed worker becomes
    data to filter, not an exception that kills the whole batch."""

    ok: bool
    value: str | None
    error: str | None = None


class AgentsCapability:
    """Sub-agents: single LLM workers the orchestrator fans out over data so the
    bulk work never enters its own context. Concurrency, limits, and retries are
    owned here (the broker), not in agent code.

    Two caps apply: `max_per_call` bounds a single `map_agents` fan-out, and
    `session_cap` bounds the total number of sub-agents spawned over the whole
    session (across every `agent`/`map_agents` call)."""

    name = "agents"

    def __init__(
        self,
        llm,
        default_tier: str = "cheap",
        max_per_call: int = 64,
        session_cap: int = 256,
    ):
        self.llm = llm
        self.default_tier = default_tier
        self.max_per_call = max_per_call
        self.session_cap = session_cap
        self._spawned = 0
        self._lock = threading.Lock()

    def exports(self) -> dict:
        return {"agent": self.agent, "map_agents": self.map_agents}

    def _reserve(self) -> None:
        with self._lock:
            if self._spawned >= self.session_cap:
                raise SubAgentLimitExceeded(
                    f"session sub-agent cap reached ({self.session_cap})"
                )
            self._spawned += 1

    def _complete(self, task: str, tier: str | None, context: str | None) -> str:
        content = task if context is None else f"{task}\n\nContext:\n{context}"
        completion = self.llm.complete(
            system=WORKER_SYSTEM,
            messages=[{"role": "user", "content": content}],
            tier=tier or self.default_tier,
        )
        return completion.text

    def agent(self, task: str, tier: str | None = None, context: str | None = None) -> str:
        self._reserve()
        return self._complete(task, tier, context)

    def map_agents(
        self,
        tasks,
        tier: str | None = None,
        context: str | None = None,
        max_concurrency: int = 8,
    ) -> list[Result]:
        tasks = list(tasks)
        if len(tasks) > self.max_per_call:
            raise ValueError(
                f"{len(tasks)} sub-agents requested; per-call limit is {self.max_per_call}"
            )

        def work(task: str) -> Result:
            try:
                self._reserve()
            except SubAgentLimitExceeded as exc:
                return Result(False, None, repr(exc))
            last = ""
            for attempt in range(2):
                try:
                    return Result(True, self._complete(task, tier, context))
                except Exception as exc:  # noqa: BLE001 - errors become data
                    last = repr(exc)
            return Result(False, None, last)

        results: list[Result | None] = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            futures = {pool.submit(work, t): i for i, t in enumerate(tasks)}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
        return results  # type: ignore[return-value]
