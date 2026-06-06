from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    """Raised when a metered action would run past the session's spend limit."""


@dataclass
class Budget:
    """The single accumulator for LLM spend in a session.

    Every LLM call records its usage here (see llm.client). The broker checks
    `check()` before metered actions so agent-initiated work fails fast when the
    limit is reached, rather than silently overspending.
    """

    limit_usd: float | None = None
    spent_usd: float = 0.0
    calls: int = 0
    by_model: dict[str, float] = field(default_factory=dict)

    def record(self, model: str, cost_usd: float) -> None:
        self.spent_usd += cost_usd
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0.0) + cost_usd

    def remaining(self) -> float:
        if self.limit_usd is None:
            return float("inf")
        return max(0.0, self.limit_usd - self.spent_usd)

    def check(self) -> None:
        if self.limit_usd is not None and self.spent_usd >= self.limit_usd:
            raise BudgetExceeded(
                f"budget exhausted: spent ${self.spent_usd:.4f} of ${self.limit_usd:.2f}"
            )
