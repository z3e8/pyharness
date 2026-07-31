# AGENTS.md

Context for AI coding agents working in this repo. Keep this file to
**non-inferable** facts (commands, layout, conventions, gotchas) — not things
you can read from the code. Universal working-style rules live in the global
`~/.claude/CLAUDE.md`, loaded automatically. The default change workflow lives
in the global `dev-workflow` skill; this file adds the pyharness-specific
commands, layout, and conventions it needs.

## What this is

`pyharness` — an AI agent whose **action space is Python**. The orchestrator
either replies with text or emits one `run_python` call that the harness runs in
a persistent Jupyter-style kernel. No fine-grained JSON tools; capabilities are
builtins in scope or tools imported on demand. See `docs/explanation/` for the
model.

## Commands

Everything is driven by `make` (`make help` lists all targets). `uv` is the
package manager; there is no `pip`/`venv` step to run by hand.

```bash
make setup     # create .env + install editable package + dev toolchain (one-time)
make test      # tests + the adversarial suite — no API key needed. Use this to verify changes.
make evals     # run the adversarial suite alone and rewrite evals/SCOREBOARD.md (commit the diff)
make lint      # ruff check + ruff format --check (what CI enforces)
make format    # ruff format + ruff check --fix (apply autofixes)
make typecheck # mypy — lenient, non-blocking (see [tool.mypy] in pyproject.toml)
make run       # interactive agent + live viewer :6061 (needs ANTHROPIC_API_KEY in .env)
make watch     # live viewer alone (tails .sessions/) for a session started elsewhere
make up        # optional Phoenix OTel backend (:6006); make down stops it
```

The dev toolchain (ruff, mypy, pytest) is pinned in the `dev` dependency group
(`[dependency-groups]` in `pyproject.toml`); `make setup` installs it. CI
(`.github/workflows/`) runs `make test` across Python 3.11/3.12/3.13 and gates on
`ruff`; `mypy` runs as a non-blocking signal. Do not wire coverage into the
default `pytest` run — `pytest-cov` breaks the sandbox child's re-exec (6 tests).

To test-drive the harness itself (run a probe task, inspect what happened, fix,
re-run) use the headless CLI — the `harness-loop` skill has the full loop:

```bash
uv run pyharness run "probe task" --json   # headless one-shot; digest on stdout, exit code = outcome (needs API key)
uv run pyharness show [--transcript]       # inspect the latest session — no API key, low context
```

Direct equivalents without make: `uv run pytest -q`, `uv run pyharness`. Config
lives in a single `.env` (copied from `.env.example`); the Makefile and
docker-compose both read it — do not export env vars by hand.

## Repo map

Folder names are visible from `ls`; this table is the *meaning* — where the
load-bearing seams are.

| Path | What lives here |
|------|-----------------|
| `pyharness/core/` | `session.py`, `agent.py`, `kernel.py`, `workspace.py`, `media.py` (image blocks back to the model), `session_venv.py` (per-session venv, out-of-process only) — the orchestration loop and persistent kernel |
| `pyharness/broker/` | `dispatch.py` — the single choke point every side effect routes through (policy → audit → budget → execute); `capabilities/` — 16 registered capabilities (browser, files, history, http, inbox, llm, notify, obs, packages, search, secrets, shell, skills, spawn, tools, web) plus three helper modules that register nothing (`exa`, `page`, `payload`); enumerate them from the live broker rather than this list — `tests/test_capability_policies.py` does; `remote/` — the out-of-process child (`child.py`, `host.py`, `protocol.py`, `sandbox.py`) |
| `pyharness/security/` | `policy.py`, `grants.py` (scoped approval grants), `vault.py`, `profiles.py` (encrypted browser login profiles), `totp.py` (RFC 6238 codes from vault seeds), `sink.py` (per-context secret masking) — action policy + the encrypted secrets vault |
| `pyharness/tools/` | `registry.py`, `skills.py` — tool discovery (`search_tools`/`use_tool`) and saved skills; `mcp/` — MCP server client/config/transport |
| `pyharness/llm/` | `client.py` — Anthropic client wrapper |
| `pyharness/cli/` | console-script entry points, one module each: `main.py` (`pyharness`), `vault.py`, `profiles.py`, `index.py`, `mcp.py` |
| `pyharness/obs/` | read-side observability: `transcript.py` (shared session views: digest, flattened transcript, outcome vocabulary), `index.py` (derived SQLite session index), `watch.py` (live session viewer, `pyharness-watch`), `telemetry.py` (opt-in OTel export), `trace.py` |
| `pyharness/` (top) | `audit.py` (hash-chained log), `budget.py`, `util.py`, `reflect.py` + `lessons.py` (post-session self-improvement pass, opt-in) |
| `tests/` | pytest suite; `mcp_server_fake.py` is a test double |
| `evals/` | the adversarial suite — `scoreboard.py` (scoring model), `support.py` (verdict helpers + offline network fixtures), `attacks/` (one module per defended claim), `run.py` (`make evals`), `SCOREBOARD.md` (the committed artifact). Runs under `make test` too |
| `deploy/observability/` | docker-compose for the optional OTel backends: Phoenix, and Langfuse (heavier profile) |
| `docs/` | documentation — explanation / how-to / reference (see `docs/index.md`) |
| `.claude/skills/docs/` | the `docs` skill: how to use and maintain the docs |
| `.claude/skills/harness-loop/` | the `harness-loop` skill: run→observe→fix loop for coding agents testing the harness |

## Conventions

- Python ≥ 3.11. Match surrounding style; keep changes surgical.
- Every side effect the agent takes goes through `broker/dispatch.py` — add new
  capabilities there, not by scattering I/O across modules.
- The audit log is a tamper-evident hash chain; verify with
  `make verify-audit DIR=.sessions/<name>`.
- A change that alters behavior, a builtin, a flag, or a config var updates the
  matching `docs/` page in the same commit — reference pages track specific
  sources (`builtins.md` ↔ `SYSTEM_PROMPT` in `agent.py`, `configuration.md` ↔
  `.env.example`, `python-api.md` ↔ `__init__.py`). The `docs` skill has the
  full sync map.

## The agents/ folder

`agents/` is version-tracked working space for agents (Claude Code included) to
plan work that outlasts one session. If a task is too big to finish in one go,
drop the plan and running progress here as a living TODO so the next agent
resumes from it, and keep it current with what actually shipped. Keep it tidy:
prune stale notes, and move finished or superseded files into `agents/old/`.
It is committed (so plans survive across machines and sessions), but nothing in
it is load-bearing for the running code.

Keep it **minimal by design** — a few active files, no scratch buildup. Three
standing docs anchor it: `README.md` (the index/map — keep current), `issues.md`
(**open-only** known-issues log: append suspicions and fix them together later,
then retire each resolved record to a dated `old/resolved-issues-*.md` so the
file only ever lists open work), and `decisions.md`
(append-only log of autonomous choices agents made and why). Working docs come in
two types — **plans** (`plan-*.md`) and **reports** (`*-report.md`) — living flat
at the top level; retire them to `old/` once shipped or superseded. The
`orchestrate-workflow` skill drives the fan-out-to-sub-agents loop over this
backlog.

## Gotchas

- `docs-legacy/` is gitignored local-only notes — **do not treat it as current
  or wire anything to it.** The live docs are `docs/`; agent scratch is `agents/`
  (see above).
- Session state lives under `.sessions/` (gitignored). The agent's relative
  paths resolve inside its session workspace, not the repo root.
- **Verifying a timing-sensitive test? Loop it under load — but don't saturate,
  and say so.** Several tests here coordinate real processes and threads
  (`test_spawn.py`, `test_remote_kernel.py`, `test_llm_client.py`), and a race
  in one is only provable by running it repeatedly under contention. That is the
  right method; keep two things in mind while doing it. **Leave headroom** —
  contention is what surfaces the race, and a few parallel workers gets you that
  without making an interactive machine unusable. **Report the shape of the run**
  ("40 iterations under 4-way load, ~6 min"): the dev machine is somebody's
  desktop, and an unannounced fleet of `uv run pytest` processes pinning every
  core is indistinguishable from a runaway.
- Tests that build a `RemoteKernel` **outside** the `kernel_factory` fixture own
  their own teardown. A child that ignores SIGTERM outlives a failed or
  interrupted run (it exits when its sleep ends, and stale `pyharness-sb-*` dirs
  are cleared by `_reap_stale_sandbox_dirs()` on the next start) — self-limiting,
  but it is why a killed run can leave a process behind for a minute.
