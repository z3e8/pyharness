# Budget

## One accumulator per session

`Budget` (`pyharness/budget.py`) is the single place LLM spend is tallied. Every
completion — whether from the orchestrator, an `llm()` call, or a sub-agent —
records its cost into the session's shared `Budget` via the LLM client, so
metering is centralized regardless of who made the call.

It tracks `spent_usd`, `calls`, and a `by_model` breakdown.

## How the limit is enforced

`Budget(limit_usd=...)` sets a cap (default `None` = unlimited; the CLI uses
`$5.00`). Enforcement is **fail-fast**: the [broker](broker.md) calls
`budget.check()` before every *metered* action (`llm`, `agents`, `web`, `obs`),
and the agent loop checks before each step. When `spent_usd` reaches the limit,
the next metered action raises `BudgetExceeded` rather than silently
overspending. `Budget.remaining()` exposes the same headroom for callers — the
[post-session reflection pass](../how-to/observability.md#post-session-reflection)
checks it and skips entirely once the budget is exhausted.

Because the check is before the action, the limit bounds agent-initiated work
(including fan-out via `map_agents`) — not just the orchestrator's own calls.
The check runs once per broker call, though, not once per task inside a
fan-out: a single `map_agents` call can dispatch its whole batch (up to
`max_concurrency`, capped at 64 tasks per call) before the *next* metered call
sees the exhausted budget and is blocked.

This dollar budget is separate from the sub-agent **count** cap
(`session_cap=256`, `max_per_call=64` in `pyharness/broker/capabilities/agents.py`),
which raises `SubAgentLimitExceeded` independent of spend — the two "budgets"
bound different things and don't share an accumulator.

## Tiers and pricing

The agent reasons about cost in **tiers**, not model strings
(`pyharness/llm/client.py`):

| Tier | Model | Use for | Output ceiling |
|------|-------|---------|----------------|
| `smart` | Opus | hard reasoning | 32k tokens |
| `mid` | Sonnet | middle ground | 16k tokens |
| `cheap` | Haiku | bulk / parallel work | 8k tokens |

The orchestrator itself runs on the **mid** tier by default and escalates by
delegating harder sub-tasks to `smart`. Cost is computed from per-model
input/output token rates, with cached input tokens billed at a fraction (cache
reads ~0.1×, cache writes ~1.25×). Rates live in `PRICING` and should be verified
against current Anthropic pricing.

## Seeing spend

Each turn records a budget snapshot to `trace.jsonl` and the CLI prints
`[spent $… over N calls]`. With [observability](../how-to/observability.md) on,
per-call cost and tokens ride on the trace spans, and the turn span carries the
final `spent_usd`.
