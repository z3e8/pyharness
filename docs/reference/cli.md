# CLI

Two console scripts are installed by the package (`pyproject.toml`).

## `pyharness`

Interactive agent REPL (`pyharness/cli.py`).

```bash
pyharness [SESSION_DIR]
```

- `SESSION_DIR` — where session state (audit log, trace, workspace) lives.
  Defaults to `.sessions/cli-<timestamp>`.
- Loads `.env` from the current directory (existing env vars win). **Requires
  `ANTHROPIC_API_KEY`**.
- Runs **out-of-process** with the OS sandbox on, and a **$5.00** budget limit
  per session.
- Mounts MCP servers from `.mcp.json` (override path with `PYHARNESS_MCP_CONFIG`)
  when the file exists.
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
- A turn that fails mid-stream is retried once, then aborted without crashing the
  REPL. `Ctrl-C` aborts the in-flight turn (e.g. a slow web search) and drops back
  to the prompt with history intact; `Ctrl-D` exits.

Each turn prints the streamed reply and a `[spent $… over N calls]` line.

## `pyharness-vault`

Manage the encrypted secrets file (`pyharness/cli_vault.py`).

```bash
pyharness-vault set NAME [VALUE]   # value prompted (hidden) if omitted
pyharness-vault list               # names only — never values
pyharness-vault rm NAME
```

The file is `~/.pyharness/secrets.enc` (override with `PYHARNESS_VAULT_FILE`),
sealed with `PYHARNESS_VAULT_PASSPHRASE` (else prompted). Use the same passphrase
when you run `pyharness` so the session can open it. See
[Use the secrets vault](../how-to/use-the-vault.md).

## `pyharness-profiles`

Manage encrypted browser login profiles (`pyharness/cli_profiles.py`).

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
and `make help`): `make run` wraps `pyharness`; `make dev` adds observability;
`make verify-audit DIR=.sessions/<name>` checks a session's
[audit chain](../explanation/security-and-audit.md).
