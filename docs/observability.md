# Observability

How pyharness makes a run inspectable — locally and, with the same code, in the
cloud. Two layers, by design:

1. **The durable record** — append-only `audit.jsonl` + `trace.jsonl` per session.
   Always on, local, dependency-free, and tamper-evident. This is the source of
   truth and the safety record.
2. **The telemetry layer** — OpenTelemetry traces + metrics emitted from the same
   seams, exported to a backend for search, aggregation, and cross-session
   analytics. Opt-in, fail-open, vendor-neutral.

The durable record answers "what exactly happened in *this* session?" The
telemetry layer answers "what's happening across *all* sessions — cost, latency,
denials, errors — and let me click into any one?"

## Why OpenTelemetry

[OpenTelemetry](https://opentelemetry.io) (OTel) is the vendor-neutral standard
for traces, metrics, and logs. pyharness instruments against OTel *only* — it
never imports a backend SDK. Where data lands is one env var
(`OTEL_EXPORTER_OTLP_ENDPOINT`). Swap Langfuse for Grafana, Honeycomb, Datadog,
or anything OTLP-speaking without touching code. That indirection is the point:
no lock-in, and the same binary emits to a laptop collector or a cloud one.

The default local backend is **[Phoenix](https://github.com/Arize-ai/phoenix)** —
a single open-source, OTLP-native, LLM-aware container. It shows each turn as a
trace with per-call cost/tokens/latency and needs no collector or database. For
multi-user / cloud-scale analytics there's a heavier **[Langfuse](https://langfuse.com)
+ Prometheus** profile behind the same OTLP endpoint (see below). Neither is a
hard dependency of the library, and switching between them is one env var.

> **Traces vs. metrics.** The per-call cost, tokens, and latency live as span
> *attributes*, so they show up wherever the traces go (including Phoenix).
> OTel *metrics* (aggregate counters/histograms below) are opt-in
> (`PYHARNESS_TELEMETRY_METRICS=true`) and need a metrics backend — Phoenix is
> traces-only, so leave them off for it and on for the Langfuse/Prometheus profile.

## What gets emitted

A turn becomes one **trace** (`session.id` groups a session's turns). The span
tree mirrors the orchestration, and capability calls nest under the code cell
that made them — the broker services them synchronously on the same thread, so
OTel's context nests them automatically (in-process *and* out-of-process):

```
pyharness.turn                      session.id, pyharness.task*, budget.spent_usd, budget.calls
├─ chat <model>                     gen_ai.* (system, model, in/out/cache tokens), pyharness.cost_usd, tier
├─ pyharness.code_cell              pyharness.code*
│  ├─ tool files.write              pyharness.action, decision, ok
│  └─ tool shell.bash               decision=deny, ok=false  (+ error on failures)
└─ chat <model> ...
```
`*` content attributes (task text, code) are attached only when content capture
is on — see [Security](#security-and-privacy).

**Metrics** (dimensions in parentheses):

| Metric | Type | Dimensions |
|---|---|---|
| `pyharness.llm.calls` | counter | model, tier |
| `pyharness.llm.cost_usd` | counter | model, tier |
| `gen_ai.client.token.usage` | counter | model, tier, token.type (input/output/cache_read) |
| `gen_ai.client.operation.duration` | histogram | model |
| `pyharness.tool.calls` | counter | action, decision, ok |
| `pyharness.tool.duration` | histogram | action |
| `pyharness.errors` | counter | source (llm/tool), decision |

## The seams

Instrumentation lives in [`pyharness/telemetry.py`](../pyharness/telemetry.py) and
is wired at exactly four points, each the natural chokepoint for its signal:

- `Session.run` → `turn_span` (root) + final budget snapshot.
- `Agent.run` → `code_cell_span` around each `kernel.run`.
- `AnthropicLLM.complete` → `llm_span` + token/cost/latency metrics. Covers the
  orchestrator, the `llm()` capability, and sub-agents, since all go through it.
- `Broker.call` → `tool_span` + call/denial/error metrics — every side effect.

Everything is a **no-op when disabled** and **fail-open**: telemetry setup and
export errors are swallowed, never propagated to the agent. The JSONL record is
independent and unaffected.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `PYHARNESS_TELEMETRY_ENABLED` | off | Opt in (or set the endpoint below). |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Collector endpoint; its presence also enables telemetry. |
| `OTEL_EXPORTER_OTLP_INSECURE` | — | `true` for plaintext gRPC (local only). |
| `OTEL_SERVICE_NAME` | `pyharness` | `service.name` on every span/metric. |
| `PYHARNESS_TELEMETRY_CAPTURE_CONTENT` | `true` | Attach prompt/code text to spans. Set `false` (redact) in shared/cloud envs. |
| `PYHARNESS_TELEMETRY_METRICS` | `false` | Emit OTel metrics. Off for Phoenix (traces-only); on for the Langfuse/Prometheus profile. |

Telemetry is **off unless asked for**, so nothing changes — and no export errors
appear — until you set these.

## Running it locally

One config file (`.env`) and `make` — see
[`deploy/observability/README.md`](../deploy/observability/README.md):

```bash
make setup     # creates .env from the template (once); set ANTHROPIC_API_KEY
make dev       # starts Phoenix + runs the agent → http://localhost:6006
```

`make dev` runs Phoenix (the single-container default) in the background and the
agent in the foreground. All telemetry env vars live in that one `.env`. For the
heavier cross-session/metrics stack, `make up-langfuse` instead.

## In the cloud

The app side is identical — set `OTEL_EXPORTER_OTLP_ENDPOINT` to your cloud
collector (TLS, no `INSECURE`) and `OTEL_EXPORTER_OTLP_HEADERS` for auth. The
collector config ([`otel-collector.yaml`](../deploy/observability/otel-collector.yaml))
is where you fan out to managed or self-hosted backends — again, no app change.
This is the 12-factor config story in [`deployment.md`](deployment.md) (R5).

Ship the durable `audit.jsonl` / `trace.jsonl` to object storage alongside, per
[`deployment.md`](deployment.md) — they remain the verifiable record even if the
telemetry backend is down or sampled.

## Security and privacy

- **Secrets never enter telemetry.** The capability model keeps secret *values*
  out of arguments (name-in/value-never-out, see [`secrets.md`](secrets.md)), so
  spans never carry them. The collector adds a redaction processor as
  defence-in-depth.
- **Content is opt-out per environment.** Prompts and code can hold private data.
  `PYHARNESS_TELEMETRY_CAPTURE_CONTENT=false` (or the collector's
  `strip-content` processor) runs metadata-only — tokens, cost, latency, action
  names, decisions — with no raw text. The local JSONL keeps full fidelity
  regardless.
- **Tamper-evident audit.** `audit.jsonl` is a hash chain
  (`hash = sha256(prev + entry)`); `pyharness.audit.verify_chain(path)` detects
  any edit, deletion, or reordering. Closes P2.2 in
  [`security-hardening.md`](security-hardening.md).
- **Fail-open telemetry, fail-closed audit.** Telemetry must never break or block
  the agent. The audit log is the opposite — it is written before a side effect's
  result is returned, on the privileged parent side.
