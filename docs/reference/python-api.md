# Python API

The public surface exported from `pyharness` (`pyharness/__init__.py`):
`Session`, `Agent`, `Kernel`, `Workspace`, `Budget`, `BudgetExceeded`, `Policy`,
`Decision`, `ActionCategory`, `ApprovalOutcome`, `GrantLedger`, `GrantScope`,
`Vault`, `ProfileStore`, `Registry`, `AuditLog`, `AnthropicLLM`, `Completion`,
`ToolCall`, `SpawnResult`.

Most use only `Session` and `Budget`.

## `Session`

```python
Session(
    root,                      # str | Path — session directory (audit, trace, workspace)
    *,
    llm=None,                  # defaults to AnthropicLLM(budget=...)
    budget=None,               # Budget — defaults to unlimited
    policy=None,               # Policy — defaults to requiring approval for
                               #   shell.bash (an arbitrary program),
                               #   skills.save_skill, skills.edit_skill,
                               #   packages.install, tools.add_mcp_server,
                               #   spawn.spawn (a spawned child's whole plan),
                               #   non-read-only MCP tool calls, state-changing
                               #   HTTP (POST/PUT/PATCH/DELETE), state-changing
                               #   browser actions (click/fill/fill_secret/
                               #   fill_totp/select_option/press/upload),
                               #   browser.save_profile, opening a browser with
                               #   a saved profile, and a screenshot (look)
                               #   after a secret was typed into the page —
                               #   see security-and-audit.md for the full list
    vault=None,                # Vault — defaults to Vault.from_env()
    profiles=None,             # ProfileStore — encrypted browser login profiles;
                               #   defaults to ProfileStore.from_env() (None w/o passphrase)
    registry=None,             # Registry — Session registers the external
                               #   capability tools (web/http/browser/packages),
                               #   MCP servers, and learned skills into it
    approver=None,             # Callable[[ApprovalRequest], ApprovalOutcome | bool]
    on_event=None,             # Callable[[kind, text], None] — stream events
    unsafe_in_process=False,   # True runs agent code in the host process —
                               #   test-only; the default is a sandboxed child
    mcp_config=None,           # str | Path | dict — MCP servers to mount; a
                               #   path is kept (even if absent) as the target
                               #   for add_mcp_server(save=True)
    skills_dir=None,           # defaults to ~/.pyharness/skills
    index_db=None,             # str | Path — the session index (stats/
                               #   inspect_session builtins + history preamble).
                               #   None leaves them dataless; the CLI passes
                               #   ~/.pyharness/index.db
    keep_outputs=8,            # recent cells whose full output stays in context
    max_steps=30,              # orchestrator step ceiling (a spawned child runs 15)
    tier="mid",                # orchestrator model tier
)
```

Four further keyword args (`capabilities`, `audit`, `workspace_dir`, `preamble`)
exist for the `spawn()` builtin's recursive child sessions — set by
`Session._start_child`, not by callers. A spawned child runs in a parent-side
thread; `spawn()` returns its handle and `wait()` returns its `SpawnResult`
(`ok`, `report`, `outcome`, `session`, `spent_usd`, `steps`) to the kernel that
spawned it; see [builtins](builtins.md#delegation).

- **`run(task: str) -> str`** — run one task to completion and return the final
  text answer. History persists across calls on the same `Session`.
- **`reflect() -> str | None`** — the post-session reflection pass (see
  [observability](../how-to/observability.md#post-session-reflection)). The CLI
  calls it at exit; library users opt in explicitly. Never raises.
- **`close()`** — write the `session_end` trace line and fold the session into
  the index (when configured), then tear down the child process (if
  out-of-process), any MCP connections, and any open HTTP/browser sessions.
  Idempotent, and each teardown step is isolated: one failing step is logged
  and the rest still run.

```python
from pyharness import Session, Budget

session = Session(".sessions/demo", budget=Budget(limit_usd=2.0))
try:
    print(session.run("Write fib.py, run it, and confirm the output."))
finally:
    session.close()
```

The `Session` owns the five things that must stay consistent: history, kernel,
policy + budget, audit log, and workspace. By default agent code runs in a
restricted child process while the broker, vault, and LLM client stay in the
parent (see [the broker](../explanation/broker.md)). `unsafe_in_process=True`
is a test-only escape hatch that `exec()`s agent code in the host namespace —
with the host's `os.environ` and live vault/broker objects reachable by
introspection — so never pass it outside a test.

## `Budget`

```python
Budget(limit_usd=None)         # None = unlimited
```

Fields: `spent_usd`, `calls`, `by_model`. Methods: `remaining()`,
`check()` (raises `BudgetExceeded` when exhausted). Every LLM call records into
the session's shared `Budget`. See [Budget](../explanation/budget.md).

## `Policy`

```python
Policy(*, deny=None, require_approval=None,    # sets of "<capability>.<operation>"
       approve_if=None)                        # list[Callable[[action, args, kwargs], bool]]
```

Rules match by prefix (`"files"` gates every file op); `approve_if` predicates
force approval from a call's arguments. Default is allow. `decide(...)` returns a
`Decision` (`ALLOW` / `DENY` / `APPROVE`). See
[Security & audit](../explanation/security-and-audit.md).

## `ActionCategory`

The severity the harness assigns a gated action, carried on the `ApprovalRequest`
handed to an `approver` and recorded in the audit log: `LOCAL` (stays in the
workspace), `OUTWARD` (sends off-box or acts on a remote), `IRREVERSIBLE` (a
remote effect known to be unrecoverable). See
[Security & audit](../explanation/security-and-audit.md).

## `ApprovalOutcome` and scoped grants

An `approver` may return an `ApprovalOutcome` (`DENY` / `ONCE` / `GRANT`) instead
of a bare `bool` (`True` normalizes to `ONCE`, falsy to `DENY`). `GRANT` allows
the call *and* mints a scoped grant for `request.scope` (a `GrantScope` of
`action_class` + `target` host, or `None` when the call is not grantable). The
`Broker` (`pyharness.broker.Broker` — not a top-level export) owns a
`GrantLedger`; a live grant matching a later call auto-approves it without
prompting. IRREVERSIBLE calls are never grantable. Pass a
`Broker(..., grants=GrantLedger())` to share or inspect the ledger. See
[scoped grants](../explanation/security-and-audit.md#scoped-grants--approve-a-domain-not-every-click).

## `Vault`

```python
Vault(secrets=None, env_prefix="PYHARNESS_SECRET_", file=None)
Vault.from_env()               # dict + env, plus encrypted file when configured
```

`names()` lists available secret names; `get(name)` resolves a value **in the
parent only** — it is never placed in the agent's kernel. See
[Use the secrets vault](../how-to/use-the-vault.md).

## `ProfileStore`

```python
ProfileStore(root, passphrase)
ProfileStore.from_env()          # None when PYHARNESS_VAULT_PASSPHRASE is unset (fail closed)
```

Named, encrypted browser `storage_state` blobs (persistent web identity), sealed
with the same scrypt+Fernet envelope as the vault. `names()` / `exists(name)` /
`info(name)` expose metadata only; `load(name)` returns the storage_state dict
**in the parent only** (the browser capability restores it into a context — it is
never placed in the agent's kernel). `save(name, state)` and `delete(name)` manage
the store. See
[Keep the agent logged in](../how-to/site-profiles.md).
