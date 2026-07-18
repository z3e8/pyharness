from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

WORKER_SYSTEM = (
    "You are a focused worker. Complete the single task you are given "
    "and return only the result — no preamble, no meta-commentary."
)


class WorkerLimitExceeded(Exception):
    """Raised when a session has spent its total budget of LLM workers."""


@dataclass(frozen=True)
class Result:
    """One worker outcome. Fan-out returns these so a failed worker becomes
    data to filter, not an exception that kills the whole batch."""

    ok: bool
    value: str | None
    error: str | None = None


class LLMCapability:
    """LLM-as-function: one-shot completions the orchestrator calls like any
    Python function — `llm()` for a single call, `map_llm()` to fan the same
    kind of call out over many prompts in parallel. Workers are toolless
    text-in/text-out; anything that needs to *act* is `spawn()`'s job.
    Concurrency and limits are owned here (the broker); transient-failure
    retries live in the LLM client, shared by every caller.

    Two count caps apply: `max_per_call` bounds a single `map_llm` fan-out,
    and `session_cap` bounds the total number of fan-out workers over the
    whole session."""

    name = "llm"

    def __init__(
        self,
        llm,
        default_tier: str = "cheap",
        max_per_call: int = 64,
        session_cap: int = 256,
        budget=None,
    ):
        self.llm = llm
        self.default_tier = default_tier
        self.max_per_call = max_per_call
        self.session_cap = session_cap
        # Shared session Budget, checked before each worker completion. The
        # broker's metered gate checks once at the start of the fan-out; without a
        # per-task check a single map_llm call could run up to max_per_call x
        # retries LLM calls and overshoot the limit by a large multiple before the
        # next broker call catches it. None (tests/bare use) skips the check.
        self._budget = budget
        self._spawned = 0
        self._lock = threading.Lock()

    def exports(self) -> dict:
        return {"llm": self.run, "map_llm": self.map_llm}

    def _reserve(self) -> None:
        with self._lock:
            if self._spawned >= self.session_cap:
                raise WorkerLimitExceeded(
                    f"session LLM-worker cap reached ({self.session_cap})"
                )
            self._spawned += 1

    def _complete(self, prompt: str, tier: str | None, system: str | None, context: str | None) -> str:
        # Fail fast once the session budget is exhausted, so a fan-out can't keep
        # spending past the limit. In map_llm this surfaces as a per-task error
        # (work() turns it into data); in llm() it propagates like any overrun.
        if self._budget is not None:
            self._budget.check()
        content = prompt if context is None else f"{prompt}\n\nContext:\n{context}"
        completion = self.llm.complete(
            system=system,
            messages=[{"role": "user", "content": content}],
            tier=tier or self.default_tier,
        )
        return completion.text

    def run(
        self,
        prompt: str,
        tier: str | None = None,
        system: str | None = None,
        context: str | None = None,
    ) -> str:
        return self._complete(prompt, tier, system, context)

    def map_llm(
        self,
        prompts,
        tier: str | None = None,
        system: str | None = None,
        context: str | None = None,
        max_concurrency: int = 8,
    ) -> list[Result]:
        prompts = list(prompts)
        if len(prompts) > self.max_per_call:
            raise ValueError(
                f"{len(prompts)} workers requested; per-call limit is {self.max_per_call}"
            )

        def work(prompt: str) -> Result:
            try:
                self._reserve()
            except WorkerLimitExceeded as exc:
                return Result(False, None, repr(exc))
            # No retry loop here: the LLM client already retries transient
            # stream failures with backoff, so anything that escapes is
            # deterministic (bad request, budget) — it becomes data once.
            try:
                return Result(
                    True, self._complete(prompt, tier, system or WORKER_SYSTEM, context)
                )
            except Exception as exc:  # noqa: BLE001 - errors become data
                return Result(False, None, repr(exc))

        results: list[Result | None] = [None] * len(prompts)
        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            futures = {pool.submit(work, p): i for i, p in enumerate(prompts)}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
        return results  # type: ignore[return-value]
