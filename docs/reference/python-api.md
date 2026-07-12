# Python API

The public surface exported from `pyharness` (`pyharness/__init__.py`):
`Session`, `Agent`, `Kernel`, `Workspace`, `Budget`, `BudgetExceeded`, `Policy`,
`Decision`, `ActionCategory`, `Vault`, `Registry`, `AuditLog`, `AnthropicLLM`,
`Completion`, `ToolCall`.

Most use only `Session` and `Budget`.

## `Session`

```python
Session(
    root,                      # str | Path — session directory (audit, trace, workspace)
    *,
    llm=None,                  # defaults to AnthropicLLM(budget=...)
    budget=None,               # Budget — defaults to unlimited
    policy=None,               # Policy — defaults to requiring approval for
                               #   skills.save_skill and packages.install
    vault=None,                # Vault — defaults to Vault.from_env()
    registry=None,             # Registry — defaults to the built-in tools
    approver=None,             # Callable[[ApprovalRequest], bool]
    on_event=None,             # Callable[[kind, text], None] — stream events
    out_of_process=False,      # run agent code in a sandboxed child process
    mcp_config=None,           # str | Path | dict — MCP servers to mount
    skills_dir=None,           # defaults to ~/.pyharness/skills
)
```

- **`run(task: str) -> str`** — run one task to completion and return the final
  text answer. History persists across calls on the same `Session`.
- **`close()`** — tear down the child process (if out-of-process) and any MCP
  connections.

```python
from pyharness import Session, Budget

session = Session(".sessions/demo", budget=Budget(limit_usd=2.0))
try:
    print(session.run("Write fib.py, run it, and confirm the output."))
finally:
    session.close()
```

The `Session` owns the five things that must stay consistent: history, kernel,
policy + budget, audit log, and workspace. In-process, agent code runs in the
host namespace; `out_of_process=True` runs it in a restricted child while the
broker, vault, and LLM client stay in the parent (see
[the broker](../explanation/broker.md)).

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

## `Vault`

```python
Vault(secrets=None, env_prefix="PYHARNESS_SECRET_", file=None)
Vault.from_env()               # dict + env, plus encrypted file when configured
```

`names()` lists available secret names; `get(name)` resolves a value **in the
parent only** — it is never placed in the agent's kernel. See
[Use the secrets vault](../how-to/use-the-vault.md).
