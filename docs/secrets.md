# Secrets, auth & connecting to services

> Implements the vault / secret-injection story from
> [`../agents/design.md`](../agents/design.md) §5. Read that section first; this
> doc explains the built result.

## The one rule

**No capability exposed to agent code ever returns a secret's cleartext.** The
agent references a secret *by name*; the broker resolves it in the parent and
injects it at the moment of the privileged call. The agent orchestrates *with* a
credential but never *holds* one.

```
agent code:   web_fetch("https://api.github.com/user", auth="github")
                 │  (only the NAME "github" crosses into agent code)
                 ▼
broker/parent:   token = vault.get("github")          # cleartext, parent-side only
                 GET ... Authorization: Bearer <token>
                 ▼
agent gets:      the response body — never the token
```

`Vault.get()` is deliberately **not** in the agent's kernel namespace. The only
secret-related function the agent can call is `secrets()`, which returns
**names, never values**.

## The vault

`security/vault.py`. Three backends, first hit wins on `get(name)`:

1. **in-memory dict** — `Vault({"github": "..."})`, mainly for tests/embedding.
2. **environment** — `PYHARNESS_SECRET_<NAME>` (e.g. `PYHARNESS_SECRET_GITHUB`).
3. **encrypted file** — a passphrase-sealed JSON map (see below).

`Session` builds its default vault with `Vault.from_env()`, which attaches the
encrypted-file backend only when **both** a passphrase
(`PYHARNESS_VAULT_PASSPHRASE`) and the file exist — so tests and non-interactive
runs fall back to dict + env with no passphrase needed.

Swapping in 1Password / Bitwarden / Hashicorp later means replacing a backend
behind this same `get` / `names` interface — nothing else changes.

### The encrypted file

`EncryptedFile` seals a `{name: value}` map with a passphrase:

- key = **scrypt**(passphrase, random 16-byte salt)  — stdlib `hashlib.scrypt`
- ciphertext = **Fernet** (AES-128-CBC + HMAC, from `cryptography`)
- stored as a JSON envelope so it is portable and the salt/KDF params travel
  with it; only the passphrase is needed to open it elsewhere.

A wrong passphrase **fails to decrypt** (authenticated) rather than returning
garbage. The file is written `0600`. Default location `~/.pyharness/secrets.enc`
(override with `PYHARNESS_VAULT_FILE`).

## Managing secrets — `pyharness-vault`

```
pyharness-vault set NAME [VALUE]   # value prompted (hidden) if omitted
pyharness-vault list               # names only — never values
pyharness-vault rm NAME
```

The passphrase comes from `PYHARNESS_VAULT_PASSPHRASE`, else it is prompted
(hidden). Set the **same** passphrase in the environment when you run
`pyharness`, or the REPL will prompt for it once at startup if a vault file
exists.

```bash
export PYHARNESS_VAULT_PASSPHRASE='…'
pyharness-vault set github          # prompts for the value
pyharness-vault set stripe sk_live_…
pyharness                           # the session can now inject "github"/"stripe"
```

## What the agent sees

In the kernel:

```python
secrets()                    # -> ["github", "stripe"]   (names only)
web_fetch(url, auth="github")                              # Authorization: Bearer <secret>
web_fetch(url, auth="x", auth_style="header", auth_name="X-API-Key")  # custom header
web_fetch(url, auth="x", auth_style="query", auth_name="api_key")     # ?api_key=<secret>
web_fetch(url, auth="x", auth_style="basic", user="alice")            # Basic base64(alice:<secret>)
```

The agent discovers what credentials exist with `secrets()`, then passes a name
to a capability's `auth` argument. The cleartext is fetched and attached
parent-side; it never enters the agent's context or, out-of-process, the child's
address space.

## Connecting an MCP service with a credential

A data source is just a tool plus a vault credential (design §6). In an MCP
config, reference a secret with `secret:NAME` in `env` or `headers`; it is
resolved through the vault **in the parent** when the server is mounted, so no
cleartext lives in the config file:

```json
{
  "mcpServers": {
    "github": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer secret:github"}
    }
  }
}
```

See [`mcp.md`](mcp.md) for the rest of the MCP story.

## Out-of-process

All of this works unchanged when `Session(out_of_process=True)`. `secrets()` is a
normal capability op, so the child binds a proxy and the call routes back over
IPC to the parent vault — and **only the names** cross the wire. Injection
(`web_fetch(auth=…)`, MCP `secret:`) happens entirely parent-side, so cleartext
never reaches the sandboxed child.

## What is *not* here (later)

- OAuth flows / token refresh.
- A general "use but don't view" sealing system beyond secrets (design §3, §11).
- Real secret managers (1Password / Bitwarden / Hashicorp) — the backend seam is
  ready; the adapters are not built.
