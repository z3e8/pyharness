# The broker

## One chokepoint for every side effect

The agent writes arbitrary Python, but it cannot touch the outside world
directly. Files, shell, web, LLM calls, sub-agents, tools, secrets, and package
installs are all **capabilities**, and every call to one flows through a single
object: the broker (`pyharness/broker/dispatch.py`).

For each call the broker runs, in order:

```
audit intent  →  policy check  →  budget (metered actions)  →  execute  →  audit outcome
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
- **Audit** appends two chained records per call to a tamper-evident log: an
  intent record before anything runs and an outcome record on every exit path,
  so an action killed in flight still exists in the record (see
  [Security & audit](security-and-audit.md#audit--a-tamper-evident-record)).
- **Budget** is checked before *metered* actions (`llm`, `agents`, `web`, `obs`)
  so agent-initiated spend fails fast at the limit.
- **Execute** calls the underlying capability function.

Both the denial paths and the success path are recorded. See
[Security & audit](security-and-audit.md) and [Budget](budget.md).

## How capabilities reach the kernel

Each capability (files, shell, search, web, http, browser, inbox, llm, agents,
tools, secrets, skills, packages, history, obs, notify) exports named
operations, and the broker wraps every one in a proxy that routes through the
`call()` path above. So when the agent calls `read(...)`, it is really calling
a broker proxy — the plain-looking builtin *is* the enforcement point.

Capabilities reach the agent by one of two surfacings, both broker-proxied:

- **Core capabilities** (the agent's own body — files, shell, delegation,
  discovery, reflection: `history`, `obs`, `notify` included) register with
  `core=True`; their proxies are injected into the kernel namespace as
  always-in-scope builtins.
- **External capabilities** (web, http, browser, inbox, packages) register
  with `core=False`, so they are *not* bare builtins. Instead `Broker.as_tool_module`
  packages each as a module of the same broker proxies (carrying the real
  signatures), registered in the [tool registry](../reference/builtins.md); the
  agent discovers and loads it via `search_tools`/`use_tool`, the same path as an
  MCP server or a learned skill. Gating is identical either way — the difference
  is only whether the capability is handed to the agent or discovered by it.

Tool modules that are *not* broker proxies already — MCP wrappers, installed
modules, learned skills — gate through a single funnel action, `tools.invoke`:
`use_tool` returns them as broker-gated proxies in-process, and the
out-of-process child reaches them the same way via its `RemoteToolSpec` proxy.
Modules built by `as_tool_module` are marked and passed through untouched, so
nothing gates twice. MCP calls get per-call policy from the server's declared
annotations — see [Security & audit](security-and-audit.md).

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

- **In-process** (`unsafe_in_process=True` — an explicit, test-only opt-in): the
  broker's proxies run directly in the host namespace. Fast, which is why the
  test suite uses it. Gating here buys consistency (the same prompts and audit
  trail as the child), not a hard boundary — in-process agent code could import
  pyharness and reach the raw registry, the host's `os.environ`, and the live
  vault, and a bare `open()` reaches anything the host process can, since
  `Workspace` resolves the *file builtins'* paths and does not confine the
  interpreter. Real enforcement is the out-of-process mode.
- **Out-of-process** (the default, for the CLI and library alike): agent code runs in
  a restricted **child process** (`pyharness/broker/remote/`). The child holds
  the persistent namespace and proxy stubs; the parent keeps the broker, vault,
  and LLM client. During a cell the parent blocks, servicing the child's
  capability calls one at a time over IPC — so approvals and budget pauses
  suspend the agent's script naturally (it's blocked on IPC), and **secrets never
  enter the child**. The IPC itself is asymmetric: child→parent (the untrusted
  direction) is framed as JSON, so a compromised child can't smuggle an
  arbitrary pickle back into the parent; parent→child stays on a pickle channel
  because the parent is trusted (`pyharness/broker/remote/protocol.py`).
  `RemoteError` normalizes exceptions that can't round-trip through that framing
  (e.g. `anthropic.APIStatusError`) so the child sees a faithful error either way.
  A Ctrl-C mid-cell is forwarded to the child as SIGINT: the running cell aborts
  with a `KeyboardInterrupt` (namespace preserved, like an in-process kernel) and
  the parent drains the aborted cell's leftover frames so the pipe stays in sync;
  a child that doesn't wind down promptly is killed (`SIGTERM`, then `SIGKILL`)
  and the next cell starts a fresh one.

The child is confined at the OS level too (macOS Seatbelt or Linux
Landlock+seccomp, plus POSIX rlimits) — see
[Security & audit](security-and-audit.md). The point of the boundary: the child
has no outbound network access and can't write outside its workspace (writes
*inside* the workspace are allowed, so `savefig`/`to_csv`-style libraries just
work), because every side effect that leaves the workspace goes back through
the broker in the parent.
