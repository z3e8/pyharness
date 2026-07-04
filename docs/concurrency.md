# Concurrency & durability

> How pyharness runs many agents at once, long-term. Extends
> [`deployment.md`](deployment.md) R3 (server-facing execution model). The short
> version: **no rewrite.** The broker chokepoint (`broker/dispatch.py`) and the
> `Session` / `RemoteKernel` seam are the decisions that would force a rebuild if
> they were wrong, and they aren't. Everything below is additive.

## The reframe: two levels of concurrency

"A bunch of agents running at the same time" is two different problems, and
separating them dissolves most of the design tension.

- **Within one session** the agent loop is *inherently sequential* — think → run
  a cell → read the output → think again. The next step depends on the last
  step's output, so there is nothing to parallelize. The one place parallelism
  belongs inside a session is fan-out (`map_agents`), which already exists as a
  thread pool (`broker/capabilities/agents.py`). Async wins almost nothing here.
- **Across sessions** is where many-agents-at-once actually lives, and the unit
  of concurrency is **one isolated worker/sandbox per session**, scheduled by a
  control plane.

The consequence: **async is not the unlock.** The reason this system can run
hundreds of agents at once is that each session is its own process/sandbox and a
scheduler hands them out — not an event loop. Isolation is already process-based
(it must be — we run untrusted LLM-authored code), which gives concurrency, crash
isolation, and OS-level resource caps for free. A wholesale asyncio rewrite of the
broker/kernel would fight that model, not help it.

## Where async earns its keep (and where it doesn't)

Async belongs at exactly two layers, neither of which is a rewrite:

- **The control-plane API** (the FastAPI in front). It holds many in-flight
  sessions without a thread per request. This is *new code* — write it async from
  day one.
- **Outbound I/O inside a worker** (LLM streaming, `httpx`) *can* go async so one
  worker isn't a thread per fan-out branch. Worth it eventually, not urgent.

Do **not** convert the broker / kernel / IPC core to asyncio. It is a large, risky
rewrite that buys little: isolation is process-based anyway, the inner loop is
sequential, and threads already cover fan-out. Synchronous blocking *inside* a
worker is fine — the worker is one session, and the request handler never blocks
on it because the queue decouples them.

## The worker model (R3)

One worker per active session; turns driven off a queue; results streamed back
through the existing `on_event` callback (already emits `llm_token` / `code` /
`output` — `core/agent.py`). No API request ever blocks on an agent turn.

```
   HTTP API (async, stateless)          Worker (sync, one per session)
   ───────────────────────────          ──────────────────────────────
   POST /sessions/{id}/turns  ──enqueue──▶  claim turn (FOR UPDATE SKIP LOCKED)
   GET  /sessions/{id}/events ◀──stream──   Session.run(task)  ── on_event ──▶ events
                                            persist history + workspace
                                            settle budget reservation
```

The seam to grow into this is the existing parent↔child protocol
(`broker/remote/`). `RemoteKernel` is already "a session behind `run(code) -> str`";
the worker is "a process that owns one `Session` and pulls its turns from a queue."

### Queue choice: Postgres, not Redis

Use a **Postgres-backed queue** (`SELECT … FOR UPDATE SKIP LOCKED`, or a thin lib
like pgmq) rather than Redis/RQ/arq. Reasons:

- You already need Postgres for sessions / budgets / audit (`deployment.md`), so a
  jobs table is **one fewer moving part** — no second datastore, no second
  consistency domain to reconcile.
- The job claim and the state write happen in **one transaction**, which is what
  gives you the correctness guarantee below.
- Redis-backed queues buy throughput you won't touch at beta scale. Graduate off
  Postgres only when measured throughput demands it.

(`aioredis` folded into `redis-py`; `arq` is quiet — neither is the right bet to
build on now.)

## Durability: turn-lifecycle, not durable execution engine

The tempting move is a durable-execution engine (Temporal / Hatchet / Restate) so
"nothing runs twice." It is the wrong granularity here.

Those engines make a *workflow* crash-proof by **replaying deterministic code**.
Our workload is non-deterministic LLM-authored Python mutating a live kernel
namespace — the least replayable thing there is. `design.md` §2/§7 already faced
this and chose **ephemeral kernel + resume-by-replay-of-history**: the kernel is
disposable; history + workspace are what's durable, checkpointed between turns.
That is correct, and it means the durability you want is at the **turn lifecycle**
(create → run turn → persist → destroy → resume), which a durable *queue +
transactional checkpoint* already covers. Temporal is a later option for genuinely
gnarly orchestration (long-running scheduled agents, multi-day human-in-the-loop,
multi-agent compensation) — and because R3 puts everything behind a worker seam,
it can be swapped in then without touching the broker or the agent.

### The one real exactly-once need: money & side effects

Strip away the framing and the legitimate concern is narrow: if a worker dies
mid-turn and the turn re-runs, it could **double-charge Anthropic tokens** or
re-execute a side effect (`purchase()`). The fix is **not** a runtime — it is:

- **Idempotency / reservation keys** keyed on `turn_id`, written transactionally
  in Postgres — reserve spend before the call, settle after — so a retry or a
  racing `map_agents` cannot double-spend. (This is `deployment.md` P1.2.)
- **Idempotent side-effecting capabilities** where it matters; approvals already
  render from structured args, so the dangerous ones are gated.

## Traditional vs emerging — where to sway

Split by blast radius:

- **Boring / battle-tested on the money/state/correctness path:** Postgres (state
  + queue), FastAPI, process-per-session workers, object storage. Bugs here cost
  real dollars or corrupt sessions.
- **Modern/emerging exactly where it removes the hardest problem:** the isolation
  substrate — rent E2B/Modal microVMs now, self-run Firecracker later behind the
  same one-method interface (`deployment.md` already makes this call). That is the
  one place the premium buys down the single biggest risk.

The instinct to reach for Temporal/Redis/async-everywhere is reaching for emerging
tech on the *correctness* path. Reach for it on the *isolation* path instead.

## Sequence

1. **Done — local robustness.** Fail-fast stream timeout (`llm/client.py`), REPL
   survives a failed turn with auto-retry-once (`cli.py`), and history rolls back
   on abort so the session never wedges (`core/agent.py`). Prerequisites
   regardless of architecture: a worker that hangs forever is worse than a CLI
   that does.
2. **R3 — worker model.** Wrap a `Session` in a worker process; drive turns off a
   Postgres-backed queue; stream via the existing `on_event`. Control-plane API is
   **async**; the worker stays **synchronous**. This is the many-agents unlock.
3. **R2/R4 — state checkpoint.** Persist history + workspace to Postgres / object
   store between turns; resume = replay history into a fresh kernel. Add
   turn-keyed **budget reservations** here — that's exactly-once-for-money.
4. **Only if orchestration grows:** evaluate Temporal/Hatchet behind the worker
   seam. Most likely not needed for a long time.
</content>
</invoke>
