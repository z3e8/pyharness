from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

WORKER_SYSTEM = (
    "You are a focused worker. Complete the single task you are given "
    "and return only the result — no preamble, no meta-commentary."
)

# Wall-clock bound for one worker completion, retries included. Without it a
# single llm()/map_llm call could legally block the kernel for the client's
# full 3 × attempt-deadline worst case; with it a worker fails inside 10
# minutes and the failure becomes data.
WORKER_TOTAL_DEADLINE_S = 600.0


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
        on_event=None,
    ):
        self.llm = llm
        self.default_tier = default_tier
        self.max_per_call = max_per_call
        self.session_cap = session_cap
        # Progress heartbeat sink (the session's traced event stream). Workers run
        # parent-side inside one broker call, so without this a slow completion —
        # or a long map_llm fan-out — shows only the broker's opaque action spinner
        # in the viewer and nothing at all in the interactive CLI, reading as a
        # hang. We emit `worker` events instead of streaming tokens: map_llm's
        # concurrent workers would interleave into garbage, and reusing the main
        # loop's llm_* stream events would corrupt the viewer's per-lane state.
        self._on_event = on_event or (lambda kind, text="", **extra: None)
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

    def _emit(self, text: str, **extra) -> None:
        # Observability must never break a worker call.
        try:
            self._on_event("worker", text, **extra)
        except Exception:  # noqa: BLE001
            pass

    def _reserve(self) -> None:
        with self._lock:
            if self._spawned >= self.session_cap:
                raise WorkerLimitExceeded(
                    f"session LLM-worker cap reached ({self.session_cap})"
                )
            self._spawned += 1

    def _complete(
        self,
        prompt: str,
        tier: str | None,
        system: str | None,
        context: str | None,
        max_tokens: int | None = None,
        label: str | None = None,
    ) -> str:
        # Fail fast once the session budget is exhausted, so a fan-out can't keep
        # spending past the limit. In map_llm this surfaces as a per-task error
        # (work() turns it into data); in llm() it propagates like any overrun.
        if self._budget is not None:
            self._budget.check()
        content = prompt if context is None else f"{prompt}\n\nContext:\n{context}"
        eff_tier = tier or self.default_tier
        label = label or f"llm({eff_tier})"

        def on_attempt(info: dict) -> None:
            # Worker streams show no tokens (concurrent fan-outs would
            # interleave into garbage), so the attempt callback is the one
            # window into a long or retrying call: a periodic streaming
            # heartbeat, each failure with the clock that fired, and each
            # retry as it actually launches ("start" only fires when one does).
            outcome, attempt, attempts = (
                info["outcome"],
                info["attempt"],
                info["attempts"],
            )
            if outcome == "streaming":
                self._emit(
                    f"{label} — streaming, ~{info['output_tokens']} tokens ({info['elapsed_s']:.0f}s)",
                    phase="progress",
                    tier=eff_tier,
                    attempt=attempt,
                )
            elif outcome == "failed":
                cause = info.get("clock_fired") or info.get("error")
                self._emit(
                    f"{label} — attempt {attempt}/{attempts} failed ({cause}, {info['elapsed_s']}s)",
                    phase="progress",
                    tier=eff_tier,
                    attempt=attempt,
                )
            elif outcome == "start" and attempt > 1:
                self._emit(
                    f"{label} — retry {attempt}/{attempts}",
                    phase="progress",
                    tier=eff_tier,
                    attempt=attempt,
                )

        completion = self.llm.complete(
            system=system,
            messages=[{"role": "user", "content": content}],
            tier=eff_tier,
            max_tokens=max_tokens,
            on_attempt=on_attempt,
            total_deadline_s=WORKER_TOTAL_DEADLINE_S,
        )
        return completion.text

    def run(
        self,
        prompt: str,
        tier: str | None = None,
        system: str | None = None,
        context: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        eff_tier = tier or self.default_tier
        self._emit(f"llm({eff_tier}) — working…", phase="start", tier=eff_tier)
        result = self._complete(prompt, tier, system, context, max_tokens)
        # No done marker on error: the failure surfaces via the broker's
        # action_end (ok=False) and the cell traceback.
        self._emit(f"llm({eff_tier}) — done", phase="done", tier=eff_tier)
        return result

    def map_llm(
        self,
        prompts,
        tier: str | None = None,
        system: str | None = None,
        context: str | None = None,
        contexts=None,
        max_concurrency: int = 8,
        max_tokens: int | None = None,
    ) -> list[Result]:
        prompts = list(prompts)
        if len(prompts) > self.max_per_call:
            raise ValueError(
                f"{len(prompts)} workers requested; per-call limit is {self.max_per_call}"
            )
        # `context` is one string for every worker; `contexts` pairs one per
        # prompt. Validate the pairing up front so a mistake fails before any
        # worker spends money, not as N confusing per-task errors.
        if contexts is not None:
            if context is not None:
                raise ValueError(
                    "pass either context (one string for all prompts) or "
                    "contexts (one per prompt), not both"
                )
            contexts = list(contexts)
            if len(contexts) != len(prompts):
                raise ValueError(
                    f"contexts has {len(contexts)} items for {len(prompts)} "
                    "prompts — they must pair one-to-one"
                )

        def work(index: int, prompt: str, ctx: str | None) -> Result:
            try:
                self._reserve()
            except WorkerLimitExceeded as exc:
                return Result(False, None, repr(exc))
            # No retry loop here: the LLM client already retries transient
            # stream failures with backoff, so anything that escapes is
            # deterministic (bad request, budget) — it becomes data once.
            try:
                return Result(
                    True,
                    self._complete(
                        prompt,
                        tier,
                        system or WORKER_SYSTEM,
                        ctx,
                        max_tokens,
                        label=f"map_llm[{index}]",
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - errors become data
                return Result(False, None, repr(exc))

        n = len(prompts)
        eff_tier = tier or self.default_tier
        self._emit(
            f"map_llm — 0/{n} done", phase="start", done=0, total=n, tier=eff_tier
        )
        # Throttle to ~10 progress lines regardless of fan-out size, so a large
        # batch shows steady movement without flooding either surface.
        step = max(1, n // 10)

        results: list[Result | None] = [None] * len(prompts)
        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            futures = {
                pool.submit(
                    work, i, p, contexts[i] if contexts is not None else context
                ): i
                for i, p in enumerate(prompts)
            }
            done = 0
            # as_completed runs on this (single) calling thread, so the counter
            # and the emit need no lock — the worker threads only run work().
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
                done += 1
                if done == n or done % step == 0:
                    self._emit(
                        f"map_llm — {done}/{n} done",
                        phase="progress",
                        done=done,
                        total=n,
                        tier=eff_tier,
                    )
        return results  # type: ignore[return-value]
