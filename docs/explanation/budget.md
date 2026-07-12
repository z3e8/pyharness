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
`budget.check()` before every *metered* action (`llm`, `agents`, `web`), and the
agent loop checks before each step. When `spent_usd` reaches the limit, the next
metered action raises `BudgetExceeded` rather than silently overspending.

Because the check is before the action, the limit bounds agent-initiated work
(including fan-out via `map_agents`) — not just the orchestrator's own calls.

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
