# Run with observability

*Goal: see what the agent did — live in a UI (this page, first half), and as a
queryable cross-session record the agent itself reads (the session index,
second half).*

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

## The activity stream

Beyond the turn-by-turn record (task, code, output, answer), `trace.jsonl`
carries **activity events** written as things happen, not after: `action_start`
before every broker-gated call and `action_end` on every exit (success, error,
deny), `approval_pending` before an approval prompt blocks on the human and
`approval_resolved` after, and `llm_start` before each completion (paired by
`llm_call`). A start without a matching end *is* the thing currently running —
or stuck — so a live view (or a `tail -f`) can always answer "what is it doing
right now, and what is it waiting on".

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

## The session index

Telemetry above is a live view for the *human*; the **session index** is the
durable, queryable layer — primarily for the *agent* (the `stats` /
`inspect_session` builtins and the history preamble), equally usable from a
terminal. It is one SQLite file **derived** from the canonical JSONL record
(`trace.jsonl`, `audit.jsonl`, skill journals): delete it and nothing is lost;
it rebuilds from source. Default location `~/.pyharness/index.db`
(`PYHARNESS_INDEX_DB` overrides); it spans every project whose sessions it has
seen, and the CLI refreshes it automatically when a session opens and closes.

```bash
pyharness-index                     # scan ./.sessions + every remembered root
pyharness-index --rebuild           # drop and re-derive everything
pyharness-index --schema            # tables/views reference
pyharness-index --sql "SELECT * FROM skill_stats"
pyharness-index --sql "SELECT name, task, outcome, cost_usd FROM sessions ORDER BY started DESC LIMIT 10"
```

Useful views: `skill_stats` (per-skill success rate), `skill_run_costs` (cost
of run N vs run 1 — the is-repetition-getting-cheaper metric), `error_taxonomy`
(what keeps failing), `session_costs` (daily spend). Any sqlite client works
too — the file is plain SQLite.

## Post-session reflection

After each CLI session, a separate cheap-model pass reads the transcript and
may propose **one** durable improvement: save a new skill, delta-edit an
existing one, record a lesson, or (the default) nothing. Skill writes go
through the normal approval prompt and land unverified; lessons only surface in
the preamble once observed in two distinct sessions; reflection-driven skill
changes are git-committed under the skills root so any bad self-edit is a
revert. On by default — set `PYHARNESS_REFLECT=false` to opt out. Library users
call `Session.reflect()` explicitly instead.
