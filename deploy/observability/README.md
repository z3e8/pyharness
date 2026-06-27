# Local observability stack

Backends for pyharness telemetry, run with one command. Same containers deploy to
the cloud later — only endpoints/secrets change. Design: [`docs/observability.md`](../../docs/observability.md).

## Quick start

From the repo root:

```bash
make setup        # creates .env (once); set ANTHROPIC_API_KEY in it
make up           # starts the stack, waits until Langfuse is ready
make run          # runs pyharness with telemetry wired up
```

That's it. No UI clicks, no API-key copying: Langfuse **self-provisions** a
project and login from the keys in `.env` (`LANGFUSE_*`) on first boot, and the
OTel Collector authenticates to it with the same pair. Each turn appears in
Langfuse as a trace (`turn → llm call / code cell → capability call`) with
token/cost/latency rolled up; metrics are in Prometheus.

Log in to the UI at http://localhost:3000 with `LANGFUSE_USER_EMAIL` /
`LANGFUSE_USER_PASSWORD` from `.env`.

## What's running

| Service | URL / port | Role |
|---|---|---|
| OTel Collector | localhost:4317 (gRPC), 4318 (HTTP) | OTLP intake → fan out to backends |
| Langfuse UI | http://localhost:3000 | LLM/agent traces, cost/token analytics |
| Prometheus | http://localhost:9090 | metrics store (Grafana-ready) |
| MinIO console | http://localhost:9291 | Langfuse blob store |
| (postgres / clickhouse / redis) | internal | Langfuse storage |

## Manage it

```bash
make down     # stop (keep data)
make clean    # stop and wipe data volumes
make logs     # tail stack logs
```

> The `LANGFUSE_*` and stack secrets in `.env` / `docker-compose.yml` are
> **dev-only defaults**. Override every one before exposing this beyond localhost
> (`make` reads them from `.env`).
