# Use the secrets vault

*Goal: let the agent use a credential (an API token, say) without the secret ever
entering the model's context or the audit log.*

The rule: **the agent references a secret by name; the value is resolved in the
parent and injected at the point of use.** The agent can list names via the
`secrets()` builtin but can never read a value. See
[Security & audit](../explanation/security-and-audit.md) for why.

## Three ways to provide a secret

Resolved in this order, first hit wins:

1. **In-memory** — `Session(vault=Vault(secrets={"github": "ghp_…"}))`.
2. **Environment** — `PYHARNESS_SECRET_<NAME>` (the agent references it lowercased
   as `<name>`). Good for CI.
3. **Encrypted file** — managed with `pyharness-vault`, below. Good for local dev.

## Manage the encrypted file

```bash
pyharness-vault set github          # prompts for the value (hidden)
pyharness-vault set github ghp_xxx  # or pass it inline
pyharness-vault list                # names and host bindings — never values
pyharness-vault rm github
```

- File: `~/.pyharness/secrets.enc` (override `PYHARNESS_VAULT_FILE`), sealed with
  a passphrase (`PYHARNESS_VAULT_PASSPHRASE`, else prompted; scrypt + Fernet).
- Set the **same passphrase** in the environment when you run `pyharness` so the
  session can open the file. If a file exists and no passphrase is set, the CLI
  prompts once at startup.

## Bind a secret to its host

`--host` (repeatable) binds a secret to the only host(s) it may ever travel to:

```bash
pyharness-vault set github ghp_xxx --host api.github.com --host uploads.github.com
```

A bound secret aimed anywhere else — an HTTP request or `web.fetch` to another
host, `browser.fill_secret`/`fill_totp` on a page whose host doesn't match —
is refused structurally, before anything leaves the machine. The approval
prompt then confirms a destination that is already known-good instead of being
the last line of defense against a look-alike host. Matching is by exact
hostname (case-insensitive, no wildcards — bind subdomains explicitly, same as
[approval grants](../explanation/security-and-audit.md#scoped-grants--approve-a-domain-not-every-click)).
A `--host` is canonicalized to its bare hostname, so a pasted URL
(`--host https://api.github.com/`) binds the same as `--host api.github.com`.

Bind every credential you can. A secret set without `--host` stays unbound and
works everywhere, with the approval prompt as its only destination check. Only
file-vault entries carry bindings; env (`PYHARNESS_SECRET_*`) secrets are
always unbound (in-memory `Vault(secrets=...)` entries may use the
`{"value": ..., "hosts": [...]}` form directly).

## How the agent uses it

`secrets()` is a builtin (always in scope), but the web/HTTP tools that consume a
secret are discovered and loaded first — injection is a property of the tool, not
of how it's surfaced:

```python
secrets()                                   # -> ["github", ...]  (names only, a builtin)

web = use_tool("web")
web.fetch("https://api.github.com/user", auth="github")   # value injected parent-side

# On a stateful HTTP session, the same names inject into headers or a body field:
http = use_tool("http")
s = http.open_session()
http.request(s, "POST", "https://api.example.com/login",
             json={"user": "me"}, secret_fields={"password": "example_pw"})
```

`web.fetch` and `request` share these auth styles: `bearer` (default), `header`
(`auth_name` = header), `query` (`auth_name` = param), `basic` (`user=`/
`auth_user=`). `request` adds `secret_fields={"field": "secret_name"}` to inject
into the JSON/form body. Load them with `use_tool` (`search_tools("web")` to find
them); see [Builtins](../reference/builtins.md).

Attaching a secret sends a credential off-box, so it prompts for approval the
first time — even on a `GET`, which is otherwise a free read — naming the secret
and destination host so a human can catch a token headed somewhere it shouldn't
go. Approve "all … on `<host>`" once and further authenticated calls to that same
host flow without re-prompting. See
[the policy model](../explanation/security-and-audit.md#policy--what-may-run).

## TOTP seeds (2FA)

A TOTP seed is just a vault secret — the base32 string the site shows next to
its QR code at 2FA setup ("can't scan? enter this code"). Store it under
`<site>_totp`:

```bash
pyharness-vault set github_totp     # paste the base32 seed (hidden)
```

At a login's 2FA step the agent calls `browser.fill_totp(sid, ref="e5",
secret_name="github_totp")`: the seed is resolved parent-side, the current
6-digit code derived there (RFC 6238, stdlib), and typed into the field.
Neither the seed nor the code ever reaches agent code, and both are masked out
of later page reads. `fill_totp` prompts for approval every time — releasing a
second factor is a credential release, never covered by a domain grant. With a
[site profile](site-profiles.md) keeping the agent logged in and a seed
covering re-login, an expired session no longer needs a human in the loop
beyond the approval prompt.

> Out-of-process, the child's environment is reduced to a minimal allowlist
> before any agent code runs — secret-bearing vars (and everything else not on
> the list) never reach it, so even a shell-out can't read them. See
> [configuration](../reference/configuration.md#subprocess-environment).
