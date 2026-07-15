# Local observability (optional OTel backends)

pyharness can emit OpenTelemetry traces; this is where they land. The daily
live view is the built-in `pyharness-watch` (no container) — this stack is the
opt-in post-hoc layer. Guide:
[`docs/how-to/observability.md`](../../docs/how-to/observability.md).

## Phoenix (one container)

From the repo root:

```bash
make up        # starts Phoenix
# set PYHARNESS_TELEMETRY_ENABLED=true in .env, then: make run
```

Open **http://localhost:6006** — each turn is a trace
(`turn → llm call / code cell → capability call`) with per-call cost, tokens, and
latency. [Phoenix](https://github.com/Arize-ai/phoenix) is OTLP-native and
LLM-aware, so there's no collector and no database to run — just the one
container. Traces persist in a Docker volume across restarts.

Manage it:

```bash
make down      # stop (keep data)
make clean     # stop and wipe the trace volume
make logs      # tail Phoenix logs
```

## Heavier profile: Langfuse + Prometheus (multi-user / cloud)

When you want cross-session analytics dashboards and aggregate metrics — or you're
moving toward hosting — use the Langfuse stack instead. Same app code; it just
points at a collector that fans out to Langfuse (traces) and Prometheus (metrics).

```bash
# set PYHARNESS_TELEMETRY_METRICS=true in .env to emit metrics, then:
make up-langfuse        # Langfuse http://localhost:3000 · Prometheus http://localhost:9090
make down-langfuse
```

Langfuse self-provisions its project/login from the `LANGFUSE_*` defaults in
`docker-compose.langfuse.yml`, and the collector authenticates via the OTel
`basicauth` extension — no UI clicks, no API-key copying. Those defaults are
**dev-only**; override them before any non-local use.
