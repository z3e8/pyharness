# Deploy

*Goal: run pyharness beyond an interactive laptop session.* pyharness is v0.1 and
single-box today; this is what exists and what to harden before running it
anywhere shared.

## Package

It's a standard hatchling package. Build a wheel and install it, or install from
source:

```bash
uv build                       # wheel + sdist in dist/
uv pip install -e .            # editable, for development
```

Console scripts `pyharness` and `pyharness-vault` are installed on the path.

## Run agent code in the sandbox

Always use **out-of-process** mode outside trusted local experimentation
(`Session(out_of_process=True)`; the CLI does this by default). Agent code then
runs in a restricted child while the broker, vault, and LLM client stay in the
parent. On macOS this adds Seatbelt confinement (no network, no filesystem
writes) plus POSIX resource limits.

> **Non-macOS caveat.** Seatbelt is macOS-only; on Linux only the POSIX resource
> limits apply today (no seccomp/namespace confinement yet). Add container-level
> isolation before running untrusted tasks on Linux. See
> [Security & audit](../explanation/security-and-audit.md).

## Configure

All config is env vars in one `.env` (see
[Configuration](../reference/configuration.md)). For a shared deployment:

- Set `ANTHROPIC_API_KEY` and a real `Budget` limit.
- Provide secrets via `PYHARNESS_SECRET_*` or the encrypted vault — never inline.
- **Redact content**: set `PYHARNESS_TELEMETRY_CAPTURE_CONTENT=false` so prompts
  and outputs don't ride on spans.

## Observability

The stacks under `deploy/observability/` are the intended telemetry backends: the
single Phoenix container for local, or the Langfuse + Prometheus profile for
cross-session analytics and metrics. Point `OTEL_EXPORTER_OTLP_ENDPOINT` at your
collector. Override all `LANGFUSE_*` dev defaults before any non-local use. See
[Run with observability](observability.md).

## Keep the audit trail

`audit.jsonl` is the tamper-evident source of truth. Ship it off-box and verify
its hash chain (`make verify-audit DIR=…`) as part of any deployment where the
record matters.
