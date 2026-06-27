# Local observability stack

Run pyharness's telemetry backends on your machine. Same containers deploy to the
cloud later — only endpoints/secrets change. Full design: [`docs/observability.md`](../../docs/observability.md).

## Quick start

```bash
# 1. Bring up the stack (collector + Langfuse + Postgres/ClickHouse/Redis/MinIO + Prometheus)
docker compose -f deploy/observability/docker-compose.yml up -d

# 2. Open Langfuse, create an account (first user is owner), create a project.
open http://localhost:3000

# 3. In the project: Settings -> API Keys -> create. Then wire the collector to it:
printf 'pk-lf-XXXX:sk-lf-XXXX' | base64        # -> <BASE64>
cp deploy/observability/.env.example deploy/observability/.env
#   set  LANGFUSE_OTEL_AUTH=Basic <BASE64>  in that .env, then:
docker compose -f deploy/observability/docker-compose.yml up -d otel-collector

# 4. Point pyharness at the collector and run it.
export PYHARNESS_TELEMETRY_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_EXPORTER_OTLP_INSECURE=true     # plaintext gRPC, local only
pyharness
```

Each turn shows up in Langfuse as a trace (`turn -> llm call / code cell -> capability call`)
with token/cost/latency rolled up. Metrics are in Prometheus at http://localhost:9090.

## Ports

| Service | URL |
|---|---|
| Langfuse UI | http://localhost:3000 |
| OTLP gRPC / HTTP | localhost:4317 / 4318 |
| Prometheus | http://localhost:9090 |
| MinIO console | http://localhost:9291 |

## Teardown

```bash
docker compose -f deploy/observability/docker-compose.yml down        # keep data
docker compose -f deploy/observability/docker-compose.yml down -v     # wipe data
```

> The secrets in `docker-compose.yml` are **dev-only**. Override every `CHANGE-ME`
> before exposing this anywhere but localhost.
