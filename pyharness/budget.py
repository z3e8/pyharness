from __future__ import annotations

import threading
from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    """Raised when a metered action would run past the session's spend limit."""


@dataclass
class Budget:
    """The single accumulator for LLM spend in a session.

    Every LLM call records its usage here (see llm.client). The broker checks
    `check()` before metered actions so agent-initiated work fails fast when the
    limit is reached, rather than silently overspending.

    Mutations are lock-guarded: spawned children run in parent-side threads and
    settle their slice into the parent (`absorb`) concurrently with the
    parent's own `record` calls."""

    limit_usd: float | None = None
    spent_usd: float = 0.0
    calls: int = 0
    by_model: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def record(self, model: str, cost_usd: float) -> None:
        with self._lock:
            self.spent_usd += cost_usd
            self.calls += 1
            self.by_model[model] = self.by_model.get(model, 0.0) + cost_usd

    def absorb(self, other: Budget) -> None:
        """Fold another budget's spend into this one — how a spawned child
        session's slice settles into its parent when the child closes. `other`
        may still be recording (an abandoned child force-settled at parent
        close), so snapshot its fields under its own lock first. Lock order is
        fixed — other, then self — and nothing acquires them in reverse, so
        there is no deadlock."""
        with other._lock:
            spent, calls, by_model = other.spent_usd, other.calls, dict(other.by_model)
        with self._lock:
            self.spent_usd += spent
            self.calls += calls
            for model, cost in by_model.items():
                self.by_model[model] = self.by_model.get(model, 0.0) + cost

    def remaining(self) -> float:
        if self.limit_usd is None:
            return float("inf")
        return max(0.0, self.limit_usd - self.spent_usd)

    def check(self) -> None:
        if self.limit_usd is not None and self.spent_usd >= self.limit_usd:
            raise BudgetExceeded(
                f"budget exhausted: spent ${self.spent_usd:.4f} of ${self.limit_usd:.2f}"
            )
