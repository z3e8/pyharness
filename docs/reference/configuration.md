# Configuration

All configuration is environment variables, kept in a single `.env` at the repo
root (copied from `.env.example` by `make setup`). The Makefile and docker-compose
both read it — there are no vars to export by hand. `pyharness` also loads `.env`
itself on startup.

## Required

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key. Required to run the agent; tests don't need it. |

## Session workspace

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_WORKSPACE` | a fresh `.sessions/cli-<timestamp>` | A stable session root reused across runs, so files dropped into `<root>/workspace/` (and files the agent creates there) survive between sessions. `~` expands; relative paths resolve from the repo. A path passed on the CLI (`uv run pyharness repl <path>`) overrides it; one-shot `pyharness run` ignores it (fresh dir per probe, `--dir` opts into reuse). |
| `PYHARNESS_KEEP_OUTPUTS` | `8` | How many recent cells keep their full output in the agent's context; older tool outputs are elided to a short stub (the kernel still holds every variable, and the full text stays in `trace.jsonl`). `0` or negative disables elision. |
| `PYHARNESS_KEEP_IMAGES` | `2` | How many recent image-carrying cells keep their screenshots in the agent's context; older image blocks are replaced in place with a short note naming the page. Deliberately much shorter than `PYHARNESS_KEEP_OUTPUTS`: a screenshot is ~1,500 tokens, consumed the turn it arrives, and — unlike elided text — not recoverable (`look()` captures the page as it is *now*). `0` or negative disables eviction; the API's 20-image-per-request bound still applies. |

## Live viewer

The [live session viewer](../how-to/observability.md#the-live-view-pyharness-watch)
embedded in the CLI.

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_WATCH` | `true` | Serve the live view for this session from the agent process (fail-open: a taken port falls back to an ephemeral one, and a failure to start never blocks the session). Set `false`/`0`/`no`/`off` to disable. |
| `PYHARNESS_WATCH_PORT` | `6061` | Port for the viewer (binds 127.0.0.1 only). |

## Telemetry

Off unless enabled. See [Run with observability](../how-to/observability.md).

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_TELEMETRY_ENABLED` | `false` | Master switch for the OTel export (the live viewer needs none of this). An explicit value wins: `false` keeps telemetry off even with an endpoint set. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | Collector endpoint. Enables telemetry on its own only when `PYHARNESS_TELEMETRY_ENABLED` is left unset. |
| `OTEL_EXPORTER_OTLP_INSECURE` | `true` | Plaintext gRPC (local only). Read verbatim by the OTel SDK — keep on its own line, no inline comment. |
| `OTEL_SERVICE_NAME` | `pyharness` | `service.name` on emitted spans. |
| `PYHARNESS_TELEMETRY_CAPTURE_CONTENT` | `true` | Attach full prompt/code/output text to spans. Set `false` to redact. |
| `PYHARNESS_TELEMETRY_METRICS` | `false` | Emit OTLP metrics. Only with a metrics backend (the Langfuse/Prometheus profile); Phoenix is traces-only. |

Truthy values: `1`, `true`, `yes`, `on` (case-insensitive).

## Session index & reflection

See [the session index](../how-to/observability.md#the-session-index).

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_INDEX_DB` | `~/.pyharness/index.db` | The derived SQLite session index the CLI wires into each session (the `stats`/`inspect_session` builtins and the history preamble). Delete-safe: rebuilt from the JSONL record. |
| `PYHARNESS_REFLECT` | `false` | Run the post-session reflection pass (an LLM reads the transcript at exit and may propose one skill edit or lesson) on CLI exit. Opt-in: set `true`/`1`/`yes`/`on`. |

## Secrets vault

See [Use the secrets vault](../how-to/use-the-vault.md). These are read from the
environment, **not** committed to `.env` if sensitive.

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_VAULT_PASSPHRASE` | prompted | Passphrase for the encrypted secrets file. |
| `PYHARNESS_VAULT_FILE` | `~/.pyharness/secrets.enc` | Path to the encrypted file. |
| `PYHARNESS_SECRET_<NAME>` | — | An env-backed secret the agent can reference as `<name>` (lowercased). |
| `PYHARNESS_PROFILES_DIR` | `~/.pyharness/profiles` | Directory holding encrypted browser login profiles (`<name>.enc`), sealed with `PYHARNESS_VAULT_PASSPHRASE`. See [site profiles](../how-to/site-profiles.md). |
| `PYHARNESS_IDENTITY_EMAIL` | unset (disabled) | Your base email address, enabling the `create_login` builtin: the agent can mint a new site account under the per-site plus-address `local+<host>@domain` with a generated, host-bound password. Plain config, not a secret (the address is returned to the agent in clear); the password never reaches agent code. Needs `PYHARNESS_VAULT_PASSPHRASE` set so entries can be stored. |

## LLM transport

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_LLM_IPV6` | unset (IPv4-only) | The Anthropic client binds to IPv4: `api.anthropic.com` is dual-stack, the HTTP client has no happy-eyeballs (it commits to whichever address family DNS lists first), and a flaky IPv6 path silently kills live streams mid-generation. The pin retires itself on a v6-only network (it falls back to the unpinned transport on the first connect failure), so set `true`/`1`/`yes` only to force default family selection. |

## Network egress

Outbound requests (`web.fetch` / `http.request` / `browser.goto`) always refuse
non-`http(s)` schemes and link-local targets (the cloud-metadata range
`169.254.169.254`). The check re-runs on every redirect hop (HTTP redirects are
followed manually, capped at 20) and, in the browser, on every request the page
makes. See [the egress guard](../explanation/security-and-audit.md#egress-guard--no-requests-to-the-boxs-own-network).

On the httpx paths (`http.request` / `web.fetch`, and remote MCP) the connection
is **pinned to the address the check cleared**, so a resolver that answers
differently at connect time reaches nothing new.

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_BLOCK_PRIVATE_NETWORK` | unset (off) | Also block loopback and private (RFC1918/ULA) ranges — a stricter posture that stops the agent reaching localhost/LAN services. Off by default so local dev works. Set `true`/`1`/`yes`/`on`. |
| `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` | unset | Read by httpx, not by pyharness — but their presence disables connection pinning, because the socket then goes to the proxy rather than to the vetted address. The name-based egress check still applies; the resolve-then-connect race does not close. |

## OS sandbox

OS-level confinement of agent code — no outbound network, writes jailed to the
workspace, the `$HOME` read jail — is built for **macOS** (Seatbelt) and
**Linux** (Landlock + seccomp, needing Landlock ABI 3 / kernel 6.2 or newer on
x86-64 or arm64). On a platform with neither — Windows, or a Linux kernel below
that floor — pyharness **refuses to start** by default rather than run
LLM-authored code with your full user privileges. See
[the sandbox](../explanation/security-and-audit.md#the-out-of-process-sandbox).

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_ALLOW_UNSANDBOXED` | unset (refuse) | Opt in to running agent code with **no OS sandbox** on a platform that has none (Windows, or Linux below the ABI floor). A loud one-time warning is printed on stderr; only the process boundary, the minimal subprocess environment (below), and POSIX rlimits (not on Windows) still apply. Set `true`/`1`/`yes`/`on`. Ignored where a sandbox exists, since it is always on there. |

## Subprocess environment

Subprocesses reachable by agent code — the out-of-process child kernel,
`shell.bash`, local (stdio) MCP servers — start from a minimal default-deny
allowlist (PATH/HOME/locale/TLS-trust/proxy basics), not from the parent's full
environment. See [the sandbox](../explanation/security-and-audit.md#the-out-of-process-sandbox).

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_ENV_PASSTHROUGH` | unset | Comma-separated extra variable names to pass through to those subprocesses (e.g. `DATABASE_URL,PROJ_CONFIG`). Can never admit the harness's own secret-bearing variables (`PYHARNESS_SECRET_*`, the vault passphrase, provider API keys). |

## Web search

| Variable | Default | Effect |
|----------|---------|--------|
| `EXA_API_KEY` | — | Exa API key for `web.search` (the raw ranked-list search). `web.fetch` doesn't need it. Held parent-side and scrubbed from the child sandbox like the LLM keys; never reaches agent code. |

## Email inbox

The read-only `inbox` tool (see [builtins → tools](builtins.md#reaching-the-outside-world-is-not-a-builtin)).
Connection details are plain config; the password (an app password for
Gmail/Fastmail/iCloud/Outlook) is **not** an env var — store it as the vault
secret named `imap` (`pyharness-vault set imap`), resolved parent-side and
never visible to agent code. Point the account at a dedicated agent address,
not a personal inbox.

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_IMAP_HOST` | — | IMAP server hostname. Unset, the tool fails with a pointer here. |
| `PYHARNESS_IMAP_PORT` | `993` | IMAP-over-TLS port. |
| `PYHARNESS_IMAP_USER` | — | Account login (usually the address itself). |

## MCP

| Variable | Default | Effect |
|----------|---------|--------|
| `PYHARNESS_MCP_CONFIG` | `.mcp.json` | Path to the MCP server config the CLI mounts when present, `pyharness-mcp` edits, and `add_mcp_server(save=True)` writes (see [Add a tool](../how-to/add-a-tool-or-skill.md)). |

## The heavier Langfuse profile

`make up-langfuse` starts Langfuse + Prometheus and reads its own dev defaults
(`LANGFUSE_*`) from `deploy/observability/docker-compose.langfuse.yml`. Those
defaults are **dev-only** — override them before any non-local use.
