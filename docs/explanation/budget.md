# Budget

## One accumulator per session

`Budget` (`pyharness/budget.py`) is the single place LLM spend is tallied. Every
completion — whether from the orchestrator, an `llm()` call, or a `map_llm`
worker — records its cost into the session's shared `Budget` via the LLM
client, so
metering is centralized regardless of who made the call.

It tracks `spent_usd`, `calls`, and a `by_model` breakdown.

## How the limit is enforced

`Budget(limit_usd=...)` sets a cap (default `None` = unlimited; the CLI uses
`$5.00`). Enforcement is **fail-fast**: the [broker](broker.md) calls
`budget.check()` before every *metered* action (`llm`, `web`, `obs`, `spawn`),
and the agent loop checks before each step. When `spent_usd` reaches the limit,
the next metered action raises `BudgetExceeded` rather than silently
overspending. `Budget.remaining()` exposes the same headroom for callers — the
[post-session reflection pass](../how-to/observability.md#post-session-reflection)
checks it and skips entirely once the budget is exhausted.

Because the check is before the action, the limit bounds agent-initiated work
(including fan-out via `map_llm`) — not just the orchestrator's own calls. On
top of the broker's once-per-call gate, each fan-out worker re-checks the
budget before its own completion, so a batch stops dispatching as soon as the
limit is hit (workers already in flight can still land, so a slight overshoot
of up to `max_concurrency` completions remains possible).

This dollar budget is separate from the worker **count** cap
(`session_cap=256`, `max_per_call=64` in `pyharness/broker/capabilities/llm.py`),
which raises `WorkerLimitExceeded` independent of spend — the two "budgets"
bound different things and don't share an accumulator.

A **spawned child session** gets its own `Budget` with its own limit — the
`budget_usd` argument capped by the parent's remaining headroom, defaulting to
a quarter of it — so a child can never spend past its slice while it runs.
When the child closes, its spend settles into the parent's accumulator
(`Budget.absorb`), so the parent's totals cover the whole session tree.
Children run in parent-side threads (spawning is asynchronous), so the
accumulator is lock-guarded; the budget slice doubles as the cancel lever —
closing the parent drops a running child's limit to what it already spent, so
its next check ends it at a step boundary. Spawns carry their own count cap
too (16 per session, `pyharness/broker/capabilities/spawn.py`).

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

## Caching and stream reliability

The client (`pyharness/llm/client.py`) sets prompt-cache breakpoints on every
request: one on the system prompt (which also covers the tool definitions
rendered before it) and one on the message history — the last message, or, once
output elision starts rewriting mid-history, the elision frontier the agent
passes as `cache_anchor`. Repeat prefixes then bill at cache-read rates instead
of full price, step over step.

The client consumes the raw event stream (never the SDK's `text_stream`, which
drops everything but text deltas): text deltas stream to the display, and
summarized adaptive-thinking deltas stream as `llm_thinking` events — so a
thinking span shows as the model working, not dead air. A per-attempt watchdog
supervises the stream: no event within `STALL_TIMEOUT_S`, or a whole attempt
running past `ATTEMPT_DEADLINE_S`, closes the connection and raises a
retryable `StreamStalled`. This is the authoritative stall detector — the
httpx read timeout only bounds byte gaps, which SSE pings reset, so it can
neither catch a wedged-but-pinging stream nor tell a healthy quiet gap from a
dead one.

Every streamed completion is also retried on transient failure — watchdog
stalls, read timeouts on a fully silent stream, dropped connections,
429/529/5xx — up to 3 attempts with exponential backoff (`STREAM_ATTEMPTS`).
This covers mid-stream failures the SDK's own `max_retries` never sees, and a
retry re-reads the prefill the failed attempt already cached. Deterministic
errors (bad request, auth) raise immediately. The agent loop additionally surfaces truncation instead of hiding
it: a `max_tokens` cutoff mid-tool-call is answered with an error tool_result
(never executed) so the model retakes the step, and a `refusal` stop ends the
turn as `(stopped: refusal)`.

## Seeing spend

Each turn records a budget snapshot to `trace.jsonl` and the CLI prints
`[spent $… over N calls]`. With [observability](../how-to/observability.md) on,
per-call cost and tokens ride on the trace spans, and the turn span carries the
final `spent_usd`.
