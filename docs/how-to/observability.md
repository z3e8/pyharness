# Run with observability

*Goal: see what the agent is doing — live in a UI (this page, first half), and
as a queryable cross-session record the agent itself reads (the session index,
second half).*

## The live view: `pyharness-watch`

The primary human view. A small local page (stdlib, no container) that tails
the session's `trace.jsonl` and renders it **as it happens**, as a transcript:
the operator's task, then one turn per model completion — its prose, then a
**step card** holding the code that turn ran, the actions that code triggered
as inline chips, and the output that came back. Plus in-flight actions with
elapsed time, a sticky banner for the pending approval the session is waiting
on, errors, and running spend. Each `llm_call` also carries a collapsed
**full-prompt view** (system prompt + every message that pass) so you can see
exactly what the model saw, and each completion's summarized adaptive thinking
streams into a collapsed, expandable **thinking** block (one per turn) so quiet
spans are visibly the model reasoning. A search box and a **View** menu (which
hides whole kinds, thinking included) sit in the header.
Because the JSONL record is written synchronously by the session, the view is
real-time by construction.

**A refusal is not an error.** An action the broker denied, or one that raised
`PermissionDenied` behind the gate, renders as an amber *refused* chip rather
than a red failure — it is the harness working, and the view should not report
it as a crash.

**What it shows, and what it doesn't.** The header carries the session's cost,
its duration, and whether it is live — three numbers, not a dashboard. Per-turn
cost, latency, token splits and character counts are in `trace.jsonl` and stayed
there: on screen they were noise between the reader and what the agent actually
did. An action's elapsed time appears only when it is over half a second, since
`0.001s` on every local call says nothing.

**Reading it.** Model prose, the task, the final answer, and tool output that is
genuinely document-shaped (a table or a code fence, or two other markdown
signals agreeing) render as markdown; anything else stays verbatim monospace,
and a rendered cell keeps a `raw` toggle. The renderer builds DOM nodes and
never touches `innerHTML` — everything on this page is untrusted text, including
whatever page the agent just fetched.

**Switching sessions.** The left rail lists every root session under the watched
directory with how it ended; clicking one re-points the stream at it without a
reload. **Follow** (on by default) moves the view to a session that
starts while you are watching; picking one by hand turns it off, so a new run
cannot drag you away from what you were reading. There is a light/dark toggle
next to the wordmark.

**Sub-agents.** When the session `spawn`s a child, the viewer follows the whole
session *tree* — the child's own turns stream into a nested, collapsible panel
opened at its spawn point, with a live status dot and a spent/budget readout,
and its cost folds into the header total. Screenshots a cell staged
(`browser.look`) are persisted under `<session>/media/` and rendered inline in
the owning lane.

It starts automatically with the CLI (`make run` prints
`[watch] live view → http://127.0.0.1:6061`; disable with
`PYHARNESS_WATCH=false`, change the port with `PYHARNESS_WATCH_PORT`). To watch
from another terminal or machine-local shell instead:

```bash
pyharness-watch                     # tails .sessions/, follows the newest session
pyharness-watch .sessions/cli-...   # pin one session
pyharness-watch --port 7000
```

## Share a finished session — the static export

`--static` bakes sessions into self-contained HTML instead of serving them: one
page per session plus an `index.html`, no server, no assets, no network.

```bash
pyharness-watch .sessions/cli-... --static out/     # one session
pyharness-watch .sessions --static out/             # every session under a dir

# …and any markdown alongside them, as pages in the same site
pyharness-watch .sessions --static out/ \
  --title "…" --lede "…" \
  --doc "Adversarial suite=evals/SCOREBOARD.md"     # repeatable
```

`--doc LABEL=PATH` renders a markdown file through the viewer's own renderer and
links it from the nav on every page. That is how the eval boards ship *inside*
the site: a claim and the sessions behind it should open as one artifact, not as
a page that links back to a repo.

It is the *same page* as the live view — `obs/page.py` and `obs/assets/` own the
shell, the stylesheet and the renderer; the export only swaps the feed (a baked
event array for the SSE stream), so the two cannot drift. Four things differ,
all because a record is not a live view: screenshots are inlined as `data:` URIs
(there is no server to fetch `/media/...`), absolute paths are redacted (a trace
records the session root and the preamble names the workspace — both carry your
home directory), the clock is frozen at the session's real duration instead of
ticking from whenever the page was opened, and the live-only controls (*follow*,
*jump to latest*) are removed rather than left promising something untrue. The
session switcher survives: the list is baked into every page as links.

Spawn children are not given their own pages — they are baked into the parent's,
where the viewer already renders them as nested panels.

`make site` rebuilds the committed demo pages under `evals/demo/site/`.

Pointed at a directory of sessions it follows the most recently active *root*
session (plus any sub-agents it spawns) and switches the top-level view only
when a genuinely new root session starts — a running sub-agent never steals the
view. It reads only the trace files, so it works on live sessions started
elsewhere and on finished ones (the full history replays on load).

## Optional: OTel export (Phoenix)

For post-hoc trace exploration across sessions, pyharness can additionally
export OpenTelemetry spans. Off by default — the live view above needs none of
this.

```bash
make up         # start the Phoenix container
# then set PYHARNESS_TELEMETRY_ENABLED=true in .env and: make run
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
right now, and what is it waiting on". `llm_thinking` events carry the
completion's summarized adaptive thinking (batched chunks, streamed by the
viewer). `llm_attempt` events record an orchestrator completion's retry chain
— each failed attempt with the clock that killed it (silence/stall/deadline),
each relaunch, each recovery — so a stalling-and-retrying call reads as
attempts in the trace, not as a silent gap; healthy single-attempt calls stay
quiet. `worker` events are the progress heartbeat of an `llm()`/`map_llm`
call — a start/done pair for a single completion, throttled `N/total done`
lines through a fan-out, plus per-attempt streaming/retry/failure lines from
long or stalling worker calls — so a slow worker call shows movement instead
of reading as a hang (the broker's `action_start` spinner alone can't show
progress *within* the one call). A `spawn` (parent side, with the child dir name) and `spawned_by`
(child side) bracket a sub-agent, and `spawn_abandoned` marks a child still
running when the parent closed and outlived the cooperative stop (it is then
force-closed, with its spend-so-far settled into the parent's final numbers);
a `media` event records a screenshot persisted under `<session>/media/`.

## What controls the OTel export

Telemetry is **opt-in** and **fail-open** (it can never break the agent). It's on
when `PYHARNESS_TELEMETRY_ENABLED` is truthy; an explicit value wins, so setting
it `false` keeps telemetry off even with an endpoint configured. Only when the
switch is left unset does the mere presence of an OTLP endpoint enable it. Even
when on, telemetry pre-flights the collector endpoint at startup: if nothing is
listening (e.g. you set the switch true but haven't run `make up`), it logs one
warning and stays off for the session instead of looping export errors into your
console. The durable record is always `audit.jsonl` / `trace.jsonl`; this layer
is a queryable view on top. Relevant `.env` keys (full list in
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

## Inspect a run cheaply

The low-context loop for scripts and coding agents (Claude Code included):
launch a probe run headlessly, read a compact digest, and only then drill in.
Never bulk-read `trace.jsonl` — every `llm_call` entry embeds the full prompt
snapshot, so the raw file is orders of magnitude bigger than the session.

```bash
pyharness run "the probe task" --json    # one JSON digest on stdout, exit code = outcome
pyharness show                           # digest of the latest session (no API key)
pyharness show <name> --transcript       # flattened transcript, prompt snapshots dropped
pyharness-index --sql "SELECT ..."       # aggregate questions across sessions
```

Escalate in that order: the digest answers "did it work, what did it cost, was
anything denied"; the transcript answers "what did it actually do"; the index
answers cross-session questions; and only a targeted read of `trace.jsonl`
(e.g. one `kind` filtered with `jq`) answers the rest. Flags, the digest
schema, and exit codes are in the [CLI reference](../reference/cli.md#pyharness-run).
The `harness-loop` Claude Code skill (`.claude/skills/harness-loop/`) packages
this loop for agents working on this repo.

## Post-session reflection

After each CLI session, a separate cheap-model pass reads the transcript and
may propose **one** durable improvement: save a new skill, delta-edit an
existing one, record a lesson, or (the default) nothing. Skill writes go
through the normal approval prompt and land unverified; lessons only surface in
the preamble once observed in two distinct sessions; reflection-driven skill
changes are git-committed under the skills root so any bad self-edit is a
revert. **Off by default** — set `PYHARNESS_REFLECT=true` to opt in; the
deterministic record (skill journals, `record_skill_use`, the index views)
carries the default self-improvement loop without an LLM in the loop. Library
users call `Session.reflect()` explicitly instead.
