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
pyharness-vault list                # names only — never values
pyharness-vault rm github
```

- File: `~/.pyharness/secrets.enc` (override `PYHARNESS_VAULT_FILE`), sealed with
  a passphrase (`PYHARNESS_VAULT_PASSPHRASE`, else prompted; scrypt + Fernet).
- Set the **same passphrase** in the environment when you run `pyharness` so the
  session can open the file. If a file exists and no passphrase is set, the CLI
  prompts once at startup.

## How the agent uses it

```python
secrets()                                   # -> ["github", ...]  (names only)
web_fetch("https://api.github.com/user", auth="github")   # value injected parent-side

# On a stateful HTTP session, the same names inject into headers or a body field:
s = open_session()
request(s, "POST", "https://api.example.com/login",
        json={"user": "me"}, secret_fields={"password": "example_pw"})
```

`web_fetch` and `request` share these auth styles: `bearer` (default), `header`
(`auth_name` = header), `query` (`auth_name` = param), `basic` (`user=`/
`auth_user=`). `request` adds `secret_fields={"field": "secret_name"}` to inject
into the JSON/form body. See [Builtins](../reference/builtins.md).

> Out-of-process, secret-bearing env vars are scrubbed from the child before any
> agent code runs, so even a shell-out can't read them.
