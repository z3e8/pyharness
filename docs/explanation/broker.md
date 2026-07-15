# The broker

## One chokepoint for every side effect

The agent writes arbitrary Python, but it cannot touch the outside world
directly. Files, shell, web, LLM calls, sub-agents, tools, secrets, and package
installs are all **capabilities**, and every call to one flows through a single
object: the broker (`pyharness/broker/dispatch.py`).

For each call the broker runs, in order:

```
policy check  →  audit  →  budget (metered actions)  →  execute
```

- **Policy** decides allow / deny / require-approval for the action, identified
  as `"<capability>.<operation>"` (e.g. `files.write`, `shell.bash`). On
  require-approval the broker builds an `ApprovalRequest` — a severity category
  plus a secret-safe summary, drawn from the owning capability's `preview` hook —
  and hands it to the human `approver`. First it checks the `GrantLedger`: a live
  scoped grant matching the call's `(action-class, host)` auto-approves it (logged
  with `grant_id`) without prompting; the human can mint such a grant by answering
  the prompt. IRREVERSIBLE actions are never grant-covered. See
  [scoped grants](security-and-audit.md#scoped-grants--approve-a-domain-not-every-click).
- **Audit** appends the call to a tamper-evident log.
- **Budget** is checked before *metered* actions (`llm`, `agents`, `web`) so
  agent-initiated spend fails fast at the limit.
- **Execute** calls the underlying capability function.

Both the denial paths and the success path are recorded. See
[Security & audit](security-and-audit.md) and [Budget](budget.md).

## How capabilities reach the kernel

Each capability (files, shell, search, web, http, browser, llm, agents, tools,
secrets, skills, packages) exports named operations, and the broker wraps every
one in a proxy that routes through the `call()` path above. So when the agent
calls `read(...)`, it is really calling a broker proxy — the plain-looking
builtin *is* the enforcement point.

Capabilities reach the agent by one of two surfacings, both broker-proxied:

- **Core capabilities** (the agent's own body — files, shell, delegation,
  discovery, reflection) register with `core=True`; their proxies are injected
  into the kernel namespace as always-in-scope builtins.
- **External capabilities** (web, http, browser, packages) register with
  `core=False`, so they are *not* bare builtins. Instead `Broker.as_tool_module`
  packages each as a module of the same broker proxies (carrying the real
  signatures), registered in the [tool registry](../reference/builtins.md); the
  agent discovers and loads it via `search_tools`/`use_tool`, the same path as an
  MCP server or a learned skill. Gating is identical either way — the difference
  is only whether the capability is handed to the agent or discovered by it.

## Why a single seam

Putting policy, audit, and budget in one place — rather than scattering checks
across every I/O site — means:

- there is exactly one thing to reason about when asking "what can the agent do,
  and is it recorded?";
- new capabilities inherit policy/audit/budget for free by registering with the
  broker;
- the same interface can front either an in-process call *or* an out-of-process
  child, with nothing else changing.

## In-process vs out-of-process

- **In-process** (default for the library): the broker's proxies run directly in
  the host namespace. Simple; the agent's code shares the host process.
- **Out-of-process** (`out_of_process=True`, used by the CLI): agent code runs in
  a restricted **child process** (`pyharness/broker/remote/`). The child holds
  the persistent namespace and proxy stubs; the parent keeps the broker, vault,
  and LLM client. During a cell the parent blocks, servicing the child's
  capability calls one at a time over IPC — so approvals and budget pauses
  suspend the agent's script naturally (it's blocked on IPC), and **secrets never
  enter the child**.

The child is confined at the OS level too (macOS Seatbelt + POSIX rlimits) — see
[Security & audit](security-and-audit.md). The point of the boundary: the child
needs neither network nor filesystem-write access, because every legitimate side
effect goes back through the broker in the parent.
