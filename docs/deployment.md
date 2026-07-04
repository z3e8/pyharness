# Cloud deployment strategy

> The forward-looking plan for hosting pyharness as a served product while it
> stays an open-source library. Read [`security-hardening.md`](security-hardening.md)
> first — its P0/P1 items are the gate to safe multi-tenant hosting, and this
> doc maps the deployment phases onto them.

## The one fact that decides everything

pyharness **runs arbitrary, LLM-authored Python in a persistent kernel**. It does
*not* run inference — that's the Anthropic API over the wire — so the deployment
needs **no GPUs and almost no CPU**. What it needs is **strong per-session
isolation of untrusted code**, because every session is, by design, executing
code the model wrote.

Two consequences follow and shape the rest of this doc:

1. **Isolation is the product's hard problem, not scale.** Compute is cheap and
   bursty; the engineering is in confining it.
2. **Tokens are the entire cost story.** Sandbox compute and baseline infra round
   to zero next to Anthropic spend (see [Cost](#cost)). Cost control = the
   `Budget` reservation model, not cheaper servers.

## What we're actually deploying

Not a stateless web service. Three planes with very different scaling shapes:

| Plane | What it is | State | Scaling |
|---|---|---|---|
| **App / edge** | Website + thin API the site calls | Stateless | Trivial, scale-to-zero |
| **Control plane** | Auth, session registry, budgets, audit, routing | Stateless logic + a DB | Easy, horizontal |
| **Execution sandboxes** | One isolated runtime **per session** running the kernel | *Stateful* (kernel vars + workspace persist across turns) | The hard, metered part |

The discipline: keep planes 1–2 dumb and cheap; make plane 3 the only thing you
think hard about. The seam already exists in the codebase — `Session` +
`RemoteKernel` *is* the runtime boundary. The cloud product is "a scheduler that
spawns `RemoteKernel`s somewhere isolated." Whatever "somewhere" is stays behind
one adapter.

## The crux: isolating execution

OS confinement exists only on macOS (Seatbelt) today; on Linux the child runs
unconfined (`security-hardening.md` P0.1), degrades silently (P0.2), and policy
defaults open (P0.3). **You cannot safely host this multi-tenant on Linux until
those close — or you rent isolation from a substrate built for it.**

| Option | Isolation today | Time-to-safe-beta | Ops | Start cost | Scale cost |
|---|---|---|---|---|---|
| **Managed vendor** (E2B / Modal) | ✅ rented, production-grade | shortest | near-zero | low (per-second) | higher (~6% premium) |
| **Self-run microVMs** (Fly / Firecracker) | ⚠️ VM boundary, but you still finish P0.1 | medium | medium | low | low |
| **K8s + gVisor / Kata** | ⚠️ strong, you assemble + tune it | longest | high | medium | lowest |

**Decision: managed vendor for Phase 0/1, behind a one-method interface;
self-run microVMs as the Phase 2 cost-down escape hatch — same interface, swap
the backend.** With "fastest to a *safe* beta" as the objective this isn't close:
the vendor lets you *buy* the isolation guarantee that is otherwise the slowest,
riskiest item on the critical path, and the premium (~6% of spend) is a rounding
error against tokens.

Override only if: a hard data-residency rule forbids third-party sandboxes (→
self-run), or you launch into very high *sustained* volume where the premium
bites (a beta usually doesn't), or the spike below shows the vendor can't host
the kernel model.

## Codebase readiness — the architectural work *before* infra

The security items (P0/P1) make hosting *safe*; these make it *possible*. They
are bigger than the security list and come first. Some good seams already exist —
`on_event` streams `llm_token`/`code`/`output` (`core/agent.py`), `Workspace`
already path-jails the scratch dir, and history serializes to plain dicts
(`core/agent.py`) so it can be persisted. The gaps:

### R1 — Distribute the trust boundary (biggest item)

Today the broker (parent: holds vault cleartext, the Anthropic key, budget, LLM
client) talks to the sandboxed child over an in-host `multiprocessing.Pipe`
(`broker/remote/host.py`). A hosted product splits these across machines, which
forces a decision the codebase hasn't made:

- **(a) Whole `Session` inside the sandbox.** Simplest, but vault cleartext and
  the Anthropic key now sit inside the agent's blast radius — breaks the
  name-in/value-never-out secrets model ([`secrets.md`](secrets.md)). Reject.
- **(b) Broker stays control-plane-side, child in the sandbox, the pipe becomes
  an authenticated network channel** (mTLS gRPC/websocket). Preserves the
  security model; the cost is turning the one-at-a-time IPC protocol
  (`broker/remote/protocol.py`) into a real network protocol — auth, framing,
  reconnection, backpressure, latency. This is the right target and the largest
  single build.

Recommendation: **(b)**. The existing parent↔child message protocol is the seam
to grow into it.

### R2 — Decide the session state model (resume)

`RemoteKernel` is explicit: kernel state is in-memory and disposable, **no
durable resume** (`broker/remote/host.py`). Conversation *history* serializes
fine (persist to Postgres between turns); the live kernel *namespace* does not.
So pick, don't pretend:

- **Ephemeral V1 (recommended):** a session's kernel lives only while warm; on
  idle, persist history + workspace, destroy the sandbox. Resume replays history
  into a fresh kernel — variables are gone but the conversation continues. Cheap,
  honest, ships now.
- **True resume (later):** keep sandboxes warm (cost) or build namespace
  checkpointing (hard — arbitrary live objects don't pickle). Defer.

### R3 — Server-facing execution model

`Session.run` blocks and drives one turn at a time (`core/session.py`); the
parent blocks during a cell servicing IPC. A web API can't call it inline. Wrap
each session in a **worker/job** (one process or sandbox per active session),
drive turns via a queue, and stream results through the existing `on_event`
callback. No request handler ever blocks on an agent turn. See
[`concurrency.md`](concurrency.md) for the full worker/queue/durability design.

### R4 — Back the workspace with object storage

`Workspace.root` is a resolved local `Path` (`core/workspace.py`). The path-jail
is good; persistence isn't there. Sync the local scratch dir ↔ object store (R2):
hydrate on session start, snapshot on idle. (This is *workspace files*, not the
kernel namespace — R2 above.)

### R5 — 12-factor config + sandbox image

Env-driven config (no local-path assumptions), and a reproducible container image
for the sandbox that bakes in the interpreter + the `SessionVenv`
(`core/session_venv.py`) per-session install path.

## Recommended initial stack (Phase 0 → 1)

Scale-to-zero end to end; idle cost ≈ Postgres + storage.

- **Website + API:** Next.js on Vercel/Cloudflare → thin FastAPI control plane on
  Cloud Run / Fly / Railway. All scale-to-zero.
- **Execution:** managed sandbox vendor, one sandbox per active session; idle →
  pause/snapshot/destroy so you pay only for active turns.
- **State:** Postgres (Neon/Supabase, serverless) for sessions/budgets/users;
  object storage (Cloudflare R2 / S3) for workspace *files* + `audit.jsonl` /
  `trace.jsonl`. Hydrate the workspace from object storage on session start,
  snapshot on idle (R4). Note: this persists files and history, **not** the live
  kernel namespace — resume replays history into a fresh kernel (R2).
- **Secrets:** the existing `Vault` for agent-visible secrets (name-in,
  value-never-out per [`secrets.md`](secrets.md)); a real KMS for platform creds.
  The Anthropic key stays platform-side, never in the sandbox.
- **Observability:** OpenTelemetry traces + metrics to a collector → backend
  (self-hosted Langfuse + Prometheus by default), config-only per environment;
  ship the durable `audit.jsonl` / `trace.jsonl` to object storage as the
  verifiable record. See [`observability.md`](observability.md).

## Phased path

- **Phase 0 — Private hosted.** One control plane + managed sandboxes, single
  region, you + a few users. Goal: prove the ephemeral session lifecycle (create
  → run turns → persist history + workspace → destroy → resume by replay, R2) and
  that budgets/audit hold end-to-end.
- **Phase 1 — Public beta.** Auth, per-user budget caps, plus the security
  prerequisites below. For multi-tenant untrusted code these are the entry
  ticket, not gold-plating.
- **Phase 2 — Scale / cost-down.** If sandbox-vendor spend dominates, migrate
  plane 3 to self-run Fly Machines / Firecracker (or K8s + gVisor) behind the
  same interface. Multi-region, warm pools for fast cold-starts.

## Critical path to a *safe* beta

Optimizing for "fastest to safe" reorders the work: **security hardening first
(pure library, no cloud account needed), infra second.** The vendor removes the
infra-side isolation work, so what's left on the critical path is mostly code you
can write today.

1. **Spike: does the vendor runtime host the kernel?** Run one real `Session`
   (persistent kernel across turns, workspace hydrate/snapshot) inside E2B *and*
   Modal. Validates the whole bet before building anything. (½–1 wk)
2. **P0.3 default-deny policy** — flip `Policy` to allowlist. Highest-leverage
   safety fix, self-contained.
3. **P0.2 fail-loud** — refuse to start a session if confinement isn't active.
4. **P1.4 limits + P1.3 egress allowlist** — memory/CPU/wall-clock caps and a
   per-session host allowlist. Bound blast radius *and* runaway cost.
5. **P1.2 budget reservation** — so concurrent `map_agents` can't race past a
   user's spend cap. Primary defense for your Anthropic key.
6. **R1 — distribute the trust boundary** (broker control-plane-side, child in
   sandbox, pipe → authenticated network channel). The largest single build; the
   vendor spike (1) informs it.
7. **R3 + R4 — worker model + object-store workspace**, then the control plane:
   create → run turn (streamed via `on_event`) → persist history + workspace →
   destroy → resume by replay (R2). Stateless API, Postgres, R2.
8. **R5 + auth + per-user budget caps + website.**

Items 2–5 are pure library work, need no cloud account, and are the gate to
"safe." Items R1/R3/R4 are the gate to "possible" — sequence them against the
vendor spike, since (1) decides how much of R1 the substrate gives you for free.

## Open-source vs hosted (open-core)

The repo stays the **harness + CLI + library** (what people self-host). The
**hosting glue** — control plane, sandbox orchestration, billing, website — lives
in a separate module/repo. Don't let cloud-specific code leak into the OSS
package; the only contract between them is `Session` / `RemoteKernel`.

## Cost

Three buckets, orders of magnitude apart.

**1. Idle baseline — ~$0–25/mo.** Everything scales to zero; the floor is R2
storage. An empty beta is nearly free.

**2. Active sandbox compute — single-digit cents/session.** During a turn the
sandbox mostly *waits on the Anthropic API*; the kernel burns CPU only for brief
`run_python` executions. Per active-hour, 1 vCPU / 2 GB:

| Substrate | ~$/active-hr | Idle |
|---|---|---|
| Fly Machines (self-run) | ~$0.003 | free when stopped |
| Modal | ~$0.10–0.15 | scale-to-zero |
| E2B | ~$0.05–0.10 | auto-pause |

**3. Anthropic tokens — the whole story.** A non-trivial session runs ~$0.10 to a
few dollars, depending on context churn and whether `map_agents` uses cheaper
models for bulk work. One to two orders of magnitude above buckets 1–2.

**Worked scenario — 100 beta users, ~5 sessions/wk, ~$0.75 tokens/session:**

| Line | Monthly |
|---|---|
| Tokens (100 × 5 × 4 × $0.75) | **~$1,500** |
| Sandbox compute (managed, ~2,000 × $0.05) | ~$100 |
| Baseline infra | ~$25 |
| **Total** | **~$1,600 (~94% tokens)** |

Design consequences:

- **Make tokens pass-through or hard-capped.** `Budget` + the P1.2 reservation
  fix *is* the cost control. Per-user caps are what stand between you and a
  surprise bill from one runaway `map_agents` loop.
- **The vendor premium (~$100, ~6%) is noise against tokens.** Self-hosting
  compute early saves no real money and adds P0.1 to the critical path — which is
  the whole case for renting isolation to reach a safe beta faster.
