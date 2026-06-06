from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

WORKER_SYSTEM = (
    "You are a focused worker sub-agent. Complete the single task you are given "
    "and return only the result — no preamble, no meta-commentary."
)


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
    owned here (the broker), not in agent code."""

    name = "agents"

    def __init__(self, llm, default_tier: str = "cheap", max_subagents: int = 64):
        self.llm = llm
        self.default_tier = default_tier
        self.max_subagents = max_subagents

    def exports(self) -> dict:
        return {"agent": self.agent, "map_agents": self.map_agents}

    def agent(self, task: str, tier: str | None = None, context: str | None = None) -> str:
        content = task if context is None else f"{task}\n\nContext:\n{context}"
        completion = self.llm.complete(
            system=WORKER_SYSTEM,
            messages=[{"role": "user", "content": content}],
            tier=tier or self.default_tier,
        )
        return completion.text

    def map_agents(
        self,
        tasks,
        tier: str | None = None,
        context: str | None = None,
        max_concurrency: int = 8,
    ) -> list[Result]:
        tasks = list(tasks)
        if len(tasks) > self.max_subagents:
            raise ValueError(
                f"{len(tasks)} sub-agents requested; limit is {self.max_subagents}"
            )

        def work(task: str) -> Result:
            last = ""
            for attempt in range(2):
                try:
                    return Result(True, self.agent(task, tier=tier, context=context))
                except Exception as exc:  # noqa: BLE001 - errors become data
                    last = repr(exc)
            return Result(False, None, last)

        results: list[Result | None] = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            futures = {pool.submit(work, t): i for i, t in enumerate(tasks)}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
        return results  # type: ignore[return-value]
