# Run with observability

*Goal: watch the agent's turns, prompts, LLM calls, and capability calls in a UI,
with per-call cost, tokens, and latency.*

## Default: Phoenix (one container)

```bash
make dev        # = make up (Phoenix, background) + make run (the agent)
```

Open **http://localhost:6006**. Each turn is one trace:

```
turn → code cell → llm call / capability call
```

[Phoenix](https://github.com/Arize-ai/phoenix) is OTLP-native and LLM-aware, so
there's no separate collector or database — just the one container. Traces
persist in a Docker volume across restarts.

Manage it:

```bash
make down       # stop, keep data
make clean      # stop and wipe the trace volume
make logs       # tail Phoenix logs
```

## What controls it

Telemetry is **opt-in** and **fail-open** (it can never break the agent). It's on
when `PYHARNESS_TELEMETRY_ENABLED` is truthy *or* an OTLP endpoint is set. The
durable record is always `audit.jsonl` / `trace.jsonl`; this layer is a queryable
view on top. Relevant `.env` keys (full list in
[Configuration](../reference/configuration.md)):

| Key | Effect |
|-----|--------|
| `PYHARNESS_TELEMETRY_ENABLED` | master opt-in |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | where traces land (default `http://localhost:4317`) |
| `PYHARNESS_TELEMETRY_CAPTURE_CONTENT` | attach full prompt/code/output text — set `false` to redact |
| `PYHARNESS_TELEMETRY_METRICS` | emit metrics (needs a metrics backend, below) |

> **Redact in the cloud.** Prompts and outputs can carry private data. Keep
> `CAPTURE_CONTENT=true` locally for debugging; set it `false` anywhere shared.

## Heavier profile: Langfuse + Prometheus

For cross-session analytics and aggregate metrics (or moving toward hosting), use
the Langfuse stack — same app code, pointed at a collector that fans out to
Langfuse (traces) and Prometheus (metrics):

```bash
# set PYHARNESS_TELEMETRY_METRICS=true in .env first, then:
make up-langfuse    # Langfuse http://localhost:3000 · Prometheus http://localhost:9090
make down-langfuse
```

Langfuse self-provisions from the `LANGFUSE_*` defaults in
`deploy/observability/docker-compose.langfuse.yml`. Those are **dev-only** —
override before any non-local use.
