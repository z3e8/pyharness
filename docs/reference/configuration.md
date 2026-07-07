# Configuration

All configuration is environment variables, kept in a single `.env` at the repo
root (copied from `.env.example` by `make setup`). The Makefile and docker-compose
both read it — there are no vars to export by hand. `pyharness` also loads `.env`
itself on startup.

## Required

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key. Required to run the agent; tests don't need it. |

## Telemetry

Off unless enabled. See [Run with observability](../how-to/observability.md).

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_TELEMETRY_ENABLED` | `true` in template | Opt-in flag. Telemetry is also enabled if an OTLP endpoint is set. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | Collector endpoint (its presence alone enables telemetry). |
| `OTEL_EXPORTER_OTLP_INSECURE` | `true` | Plaintext gRPC (local only). Read verbatim by the OTel SDK — keep on its own line, no inline comment. |
| `OTEL_SERVICE_NAME` | `pyharness` | `service.name` on emitted spans. |
| `PYHARNESS_TELEMETRY_CAPTURE_CONTENT` | `true` | Attach full prompt/code/output text to spans. Set `false` to redact. |
| `PYHARNESS_TELEMETRY_METRICS` | `false` | Emit OTLP metrics. Only with a metrics backend (the Langfuse/Prometheus profile); Phoenix is traces-only. |

Truthy values: `1`, `true`, `yes`, `on` (case-insensitive).

## Secrets vault

See [Use the secrets vault](../how-to/use-the-vault.md). These are read from the
environment, **not** committed to `.env` if sensitive.

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_VAULT_PASSPHRASE` | prompted | Passphrase for the encrypted secrets file. |
| `PYHARNESS_VAULT_FILE` | `~/.pyharness/secrets.enc` | Path to the encrypted file. |
| `PYHARNESS_SECRET_<NAME>` | — | An env-backed secret the agent can reference as `<name>` (lowercased). |

## MCP

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_MCP_CONFIG` | `.mcp.json` | Path to the MCP server config the CLI mounts (see [Add a tool](../how-to/add-a-tool-or-skill.md)). |

## The heavier Langfuse profile

`make up-langfuse` starts Langfuse + Prometheus and reads its own dev defaults
(`LANGFUSE_*`) from `deploy/observability/docker-compose.langfuse.yml`. Those
defaults are **dev-only** — override them before any non-local use.
