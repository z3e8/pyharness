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
pyharness-vault get github          # print the VALUE — human use only (pipe to pbcopy)
pyharness-vault rm github
```

- File: `~/.pyharness/secrets.enc` (override `PYHARNESS_VAULT_FILE`), sealed with
  a passphrase (`PYHARNESS_VAULT_PASSPHRASE`, else prompted; scrypt + Fernet).
- Set the **same passphrase** in the environment when you run `pyharness` so the
  session can open the file. If a file exists and no passphrase is set, the CLI
  prompts at startup and checks what you type against the file, re-prompting up
  to three times — a typo is caught there rather than surfacing mid-task as a
  decryption failure inside agent code. A passphrase you set in the environment
  yourself is trusted as configured and not re-checked; if it is wrong, the
  first secret to be resolved fails with a message naming
  `PYHARNESS_VAULT_PASSPHRASE`.

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
works everywhere, with the approval prompt as its only destination check — a
weaker check, because it is a human reading one line rather than a rule the
harness enforces. What the harness does hold for an unbound browser fill is that
the credential lands on the page that was approved: `fill_secret`/`fill_totp`
capture the page's host when the confirmation is built and refuse if the live
page has moved to a different host by the time they type, so a page that
redirects itself after you click approve gets a refusal instead of the secret.
Navigation inside the approved host (a login flow moving between its own paths)
is not refused. Only
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

## Create a new site login

*Goal: "make me an account on this site" — without the agent ever holding the
password.*

Set your base address once in `.env`:

```bash
PYHARNESS_IDENTITY_EMAIL=me@example.com
```

With that (and the vault passphrase) set, the `create_login` builtin is live.
Asked to sign up on `app.example.com`, the agent calls
`create_login("app.example.com")`, which — after a per-site approval prompt —
derives the plus-address `me+app.example.com@example.com`, generates a strong
password parent-side, and stores both in the vault bound to that host
(`app_example_com_email` / `app_example_com_password`). The agent gets back the
email in clear (it types it with `fill` and may need to read it on confirmation
pages) and the password *name* only:

```python
login = create_login("https://app.example.com/signup")   # prompts for approval
b = use_tool("browser"); b.open_browser()
b.goto("https://app.example.com/signup"); b.snapshot()
b.fill(ref="e3", value=login["email"])
b.fill_secret(ref="e4", secret=login["password_secret"])  # prompts; value never seen
```

`length` (12–64) and `symbols` (`True`, `False`, or a string of the punctuation
the site allows) accommodate site password policies; the 12-character floor is
enforced parent-side. A repeat call for the same site returns the same names
with `created=False` — existing entries are never overwritten, so rotating or
fixing a half-created login is a human act via `pyharness-vault`. To move the
password into your password manager, reveal it at the terminal:

```bash
pyharness-vault get app_example_com_password
```

After signing up, `save_profile` keeps the logged-in state for future sessions
(see [site profiles](site-profiles.md)).

## TOTP seeds (2FA)

A TOTP seed is just a vault secret — the base32 string the site shows next to
its QR code at 2FA setup ("can't scan? enter this code"). Store it under
`<site>_totp`:

```bash
pyharness-vault set github_totp     # paste the base32 seed (hidden)
```

At a login's 2FA step the agent calls `browser.fill_totp(ref="e5",
secret="github_totp")`: the seed is resolved parent-side, the current
6-digit code derived there (RFC 6238, stdlib), and typed into the field.
Neither the seed nor the code ever reaches agent code, and both are masked out
of later page reads. `fill_totp` prompts for approval every time — releasing a
second factor is a credential release, never covered by a domain grant — and,
like `fill_secret`, it refuses to type the code if the page has navigated to a
different host since that prompt. With a
[site profile](site-profiles.md) keeping the agent logged in and a seed
covering re-login, an expired session no longer needs a human in the loop
beyond the approval prompt.

> Out-of-process, the child's environment is reduced to a minimal allowlist
> before any agent code runs — secret-bearing vars (and everything else not on
> the list) never reach it, so even a shell-out can't read them. See
> [configuration](../reference/configuration.md#subprocess-environment).
