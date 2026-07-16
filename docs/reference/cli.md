# CLI

Five console scripts are installed by the package (`pyproject.toml`).

## `pyharness`

The agent CLI (`pyharness/cli/main.py`), with three subcommands: `repl` (the
default), `run`, and `show`.

### `pyharness repl`

Interactive agent REPL.

```bash
pyharness [repl] [SESSION_DIR]
```

- `SESSION_DIR` — where session state (audit log, trace, workspace) lives.
  Defaults to `.sessions/cli-<timestamp>`, or `PYHARNESS_WORKSPACE` when set
  (see [Configuration](configuration.md)); the explicit arg always wins.
- Loads `.env` from the current directory (existing env vars win). **Requires
  `ANTHROPIC_API_KEY`**.
- Runs **out-of-process** with the OS sandbox on, and a **$5.00** budget limit
  per session.
- Mounts MCP servers from `.mcp.json` (override path with `PYHARNESS_MCP_CONFIG`)
  when the file exists; the path is kept either way so the agent's
  `add_mcp_server(..., save=True)` can create it.
- If an encrypted vault file exists and no passphrase is set, prompts for it once.
- Actions that require approval print `⚠ approval required [category]: action`
  and a one-line summary of the effect (method + url + body fields, or a browser
  action with the page it lands on), then ask `allow? [y/N]`. When the action is
  grantable (state-changing on a known host, and not irreversible), it instead
  asks `allow? [y/a/N]` — `a` mints a session grant so all state-changing actions
  of that class on that host flow without re-prompting. IRREVERSIBLE actions
  (e.g. HTTP `DELETE`) and credential steps (`fill_secret`, secret-gated `look`)
  never offer `a`. See
  [scoped grants](../explanation/security-and-audit.md#scoped-grants--approve-a-domain-not-every-click).
- Agent notifications (`notify(...)`) print standalone as `[agent note] …` —
  agent-authored text, rendered distinctly from approval prompts and never
  asking for input — and are mirrored best-effort as a desktop notification.
- A turn that fails mid-stream is retried once, then aborted without crashing the
  REPL. `Ctrl-C` aborts the in-flight turn (e.g. a slow web search) and drops back
  to the prompt with history intact; `Ctrl-D` exits.

Each turn prints the streamed reply and a `[spent $… over N calls]` line.

On exit, the session is folded into the [session index](../how-to/observability.md#the-session-index)
and the [reflection pass](../how-to/observability.md#post-session-reflection)
runs (a skill proposal still asks for approval; `PYHARNESS_REFLECT=false` skips
the pass).

### `pyharness run`

One task, headless, no stdin — the entry point for scripts and coding agents
(see [Inspect a run cheaply](../how-to/observability.md#inspect-a-run-cheaply)).

```bash
pyharness run "TASK" [--dir PATH] [--budget USD] [--json] [--approve-all] [--reflect]
```

- `--dir` — session root; defaults to a fresh `.sessions/run-<timestamp>` per
  invocation (deliberately ignores `PYHARNESS_WORKSPACE` — a probe run wants a
  clean digest; a reused dir makes it cumulative). `--budget` caps spend
  (default $5).
- **Approvals are denied** (fail closed, audited like any denial) — there is no
  stdin to ask. `--approve-all` answers each request `once`; it never mints a
  standing grant. No vault passphrase prompt either: vault-backed features fail
  closed.
- Prints the final answer to stdout (a `[session …]` summary goes to stderr);
  `--json` prints the session digest instead — one JSON object with
  `session, name, outcome, answer, task, tasks, steps, llm_calls, errors,
  cost_usd, actions, denials, started, ended, trace, audit, workspace`.
- `--reflect` (or truthy `PYHARNESS_REFLECT`) runs the reflection pass;
  without `--approve-all` its skill writes are denied like any other.
- The embedded live viewer still starts (`PYHARNESS_WATCH`, URL on stderr).
- Exit code reflects the outcome:

| outcome | exit |
|---------|------|
| `answered` | 0 |
| `stopped:max_steps` | 2 |
| `stopped:budget` | 3 |
| `error` | 4 |
| `aborted` / `empty` | 5 |
| interrupted (Ctrl-C) | 130 |

### `pyharness show`

Inspect a past session from its JSONL record — pure reads, no API key.

```bash
pyharness show [SESSION] [--root DIR] [--transcript | --json]
```

- `SESSION` — a session dir path or a name under `--root` (default
  `./.sessions`); omitted, the most recently active session.
- Default output is the human digest; `--json` the same envelope as
  `run --json`; `--transcript` the flattened transcript (task, agent text,
  code, outputs, errors, skill uses, answer — the bulky per-call prompt
  snapshots in `trace.jsonl` are dropped, which is why this is the
  low-context view).

## `pyharness-vault`

Manage the encrypted secrets file (`pyharness/cli/vault.py`).

```bash
pyharness-vault set NAME [VALUE] [--host HOST ...]
                                   # value prompted (hidden) if omitted; each
                                   # --host binds injection to that host only
pyharness-vault list               # names and host bindings — never values
pyharness-vault rm NAME
```

The file is `~/.pyharness/secrets.enc` (override with `PYHARNESS_VAULT_FILE`),
sealed with `PYHARNESS_VAULT_PASSPHRASE` (else prompted). Use the same passphrase
when you run `pyharness` so the session can open it. See
[Use the secrets vault](../how-to/use-the-vault.md).

## `pyharness-mcp`

Manage the MCP server config the session mounts (`pyharness/cli/mcp.py`).

```bash
pyharness-mcp add NAME --command CMD [--arg=A]...   # local server
pyharness-mcp add NAME --url URL                    # remote server
pyharness-mcp list
pyharness-mcp rm NAME
```

- Edits `.mcp.json` in the current directory (override with
  `PYHARNESS_MCP_CONFIG`); adding never contacts the server (mounting is lazy).
- `--env K=V` / `--header K=V` values must be `secret:NAME` vault refs — a
  cleartext credential is refused. `--summary` / `--keyword` / `--category` set
  the discovery metadata `search_tools` ranks on.
- Use the `--arg=-y` form for argument values that start with a dash.

See [Add a tool or save a skill](../how-to/add-a-tool-or-skill.md) for the
config shape and the in-session alternative (`add_mcp_server`).

## `pyharness-index`

Maintain and query the [session index](../how-to/observability.md#the-session-index)
(`pyharness/cli/index.py`).

```bash
pyharness-index                    # update: scan ./.sessions + all remembered roots
pyharness-index --rebuild          # drop everything and re-derive from JSONL
pyharness-index --sql "SELECT..."  # read-only query, rows printed as JSON
pyharness-index --schema           # tables/views reference
```

The DB is `~/.pyharness/index.db` (override with `PYHARNESS_INDEX_DB`); running
the agent keeps it fresh automatically, so this CLI is for ad-hoc queries and
rebuilds.

## `pyharness-watch`

The [live session viewer](../how-to/observability.md#the-live-view-pyharness-watch)
(`pyharness/watch.py`) — a local page tailing `trace.jsonl`.

```bash
pyharness-watch                     # tails .sessions/, follows the newest session
pyharness-watch <session-dir>       # pin one session (a dir containing trace.jsonl)
pyharness-watch --port 7000         # default 6061
```

The CLI embeds the same viewer automatically (`PYHARNESS_WATCH`, default on),
so this standalone form is for watching a session started elsewhere or
replaying a finished one.

## `pyharness-profiles`

Manage encrypted browser login profiles (`pyharness/cli/profiles.py`).

```bash
pyharness-profiles list                 # names + saved-at + cookie count + domains — never values
pyharness-profiles rm NAME
pyharness-profiles login NAME [URL]     # headed browser; log in, press Enter to capture + encrypt
```

`login` opens a real browser window so you can log in yourself (2FA and all), then
saves the session state. Files live under `~/.pyharness/profiles/` (override
`PYHARNESS_PROFILES_DIR`), sealed with `PYHARNESS_VAULT_PASSPHRASE` (else prompted)
— the same passphrase as the vault. `login` needs the `pyharness[browser]` extra.
When profiles exist, `pyharness` prompts for the passphrase at startup so a session
can open them. See [Keep the agent logged in](../how-to/site-profiles.md).

## Make targets

Day-to-day you drive these through `make` (see [Configuration](configuration.md)
and `make help`): `make run` (and its alias `make dev`) wraps `pyharness`, live
viewer included; `make watch` wraps `pyharness-watch`; `make up` starts the
optional Phoenix OTel backend; `make verify-audit DIR=.sessions/<name>` checks
a session's [audit chain](../explanation/security-and-audit.md).
