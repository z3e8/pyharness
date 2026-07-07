# Security & audit

The trust boundary is simple to state: **the agent writes untrusted code; the
harness decides what that code is allowed to do, keeps secrets out of its reach,
and records everything.** Four mechanisms, all sitting at or behind
[the broker](broker.md).

## Policy — what may run

`pyharness/security/policy.py` judges each action (`"<capability>.<operation>"`)
and returns one of three decisions:

- **ALLOW** — proceed (the default; pyharness is allow-by-default).
- **DENY** — refuse and record it.
- **APPROVE** — ask a human `approver` first; refusal is recorded as denied.

Rules match by prefix, so `"files"` gates every file operation and
`"files.write"` gates just writes. The default policy requires approval for
`skills.save_skill` and `packages.install` — both write code that would run in
later sessions, so a human signs off at author time. The CLI's approver prints
the action and arguments and asks `allow? [y/N]`.

> The approver is handed the **structured** action + arguments, never an
> agent-supplied display string — so what a human sees is exactly what executes.

## Vault — secrets the agent can name but never read

`pyharness/security/vault.py` holds one hard rule: **no capability exposed to
agent code ever returns a secret's cleartext.** The agent sees only names (via
the `secrets()` builtin); the value is resolved in the parent and injected at the
point of use (e.g. `web_fetch(url, auth="github")`). `Vault.get()` is
deliberately *not* in the kernel namespace.

Resolution order, first hit wins: in-memory dict → environment
(`PYHARNESS_SECRET_<NAME>`) → encrypted file. The file is sealed with a
passphrase-derived key (scrypt) and Fernet (authenticated AES); a wrong
passphrase fails to decrypt rather than returning garbage. See
[Use the secrets vault](../how-to/use-the-vault.md).

## The out-of-process sandbox

When the agent runs in a child process (`out_of_process=True`), two OS-level
layers confine it (`pyharness/broker/remote/sandbox.py`), on top of the process
boundary itself:

- **Env scrubbing** — secret-bearing variables (`PYHARNESS_SECRET_*`, the vault
  passphrase) are deleted from the child's environment before any agent code
  runs, and from any subprocess the child spawns. So even `printenv` can't read a
  secret the parent legitimately holds.
- **macOS Seatbelt** (`sandbox-exec`) — denies exactly the two channels that
  would bypass the broker: outbound network and filesystem writes. Anything the
  child execs inherits the profile.
- **POSIX rlimits** — no core dumps; a process cap to blunt fork bombs.

Both are best-effort and degrade silently where a platform can't honor them. On
non-macOS, only the resource limits apply (seccomp/namespace confinement isn't
built yet). The child needs neither network nor disk-write to function, because
every legitimate side effect goes back through the broker in the parent.

## Audit — a tamper-evident record

`pyharness/audit.py` appends every capability call to `audit.jsonl` as a hash
chain: each entry stores `hash = sha256(prev_hash + entry)` and a `prev` pointer.
Any later edit, deletion, or reordering breaks the chain, so the log is
verifiable — which matters once it's shipped off-box.

Secrets are never arguments to capabilities (they're referenced by name and
injected in the parent), so logged arguments are safe to persist.

Verify a session's chain:

```bash
make verify-audit DIR=.sessions/<name>
# → "✓ intact"  or  "✗ broken at entry N"
```

or in code: `from pyharness.audit import verify_chain`.

`audit.jsonl` is the always-on, local source of truth; the
[observability](../how-to/observability.md) layer is a queryable view on top of
the same events, not a replacement.
