# The `run_python` action space

## The idea

Most agent frameworks give the model a menu of fine-grained JSON tools —
`read_file`, `list_dir`, `http_get`, each a separate schema the model fills in.
pyharness gives it exactly one: **`run_python`**. The orchestrator does one of two
things each step:

1. reply with plain text (a message or the final answer), or
2. emit a single `run_python` call — Python the harness executes in the session
   kernel.

There are no other tools. When the agent needs a capability, it *writes Python*:
`read(path)`, `bash(cmd)`, `map_agents(tasks)`.

## Why Python instead of JSON tools

- **Composition.** One cell can loop, branch, filter, and combine capabilities —
  `for f in search(...): process(read(f))` — instead of a round-trip per step.
  The model expresses a whole sub-plan in code the way a programmer would.
- **Token economy.** Intermediate data stays in kernel variables. Only what the
  agent `print()`s comes back into its context, so a 10k-row dataset can live in
  a variable and never cost context tokens. The one non-text exception is
  `browser.look`: it attaches a screenshot to the call's result as an image block
  the model sees (staged parent-side through a `MediaOutbox`, never across the
  child pipe) — pixels are the one thing text output can't carry.
- **A familiar action space.** Models are extraordinarily fluent in Python.
  Writing code is closer to how they were trained than filling bespoke tool
  schemas.

## The kernel is persistent

A session is a Python kernel, like Jupyter (`pyharness/core/kernel.py`). Each
`run_python` is a cell; variables, imports, and functions defined in one cell
remain available in the next. Only captured stdout/stderr is returned to the
orchestrator — everything else stays in the namespace, unseen.

This shapes how the agent is told to work: keep large or intermediate data in
variables and pass it between cells; never print it back to yourself. When a cell
errors, the traceback comes back and the next cell fixes it and *reuses the
variables already computed* rather than starting over.

> State is in-memory and disposable. If the kernel (or, out-of-process, the child)
> dies mid-session, its variables are lost — there is no durable resume yet.

## Large and binary payloads

The token-economy promise above only holds if large data can actually *reach* a
variable. There are two distinct size boundaries, and only one of them is a cap:

- **The variable boundary** (capability → kernel). A fetched page, an API
  response, a file read, a command's output — the *full* body crosses into the
  agent's Python. It is never truncated on the way in. This is what lets a cell
  parse a whole page with BeautifulSoup or page through a long log.
- **The display boundary** (kernel → context). What the agent chooses to
  `print()` back to itself is capped (`util.MAX_OUTPUT`, ~10k chars, kept
  head-and-tail so an ending survives). This is the only truncation, and it
  guards the context window, not the data.

Keeping these separate is the point: the agent holds the whole thing and decides
how much to surface. It searches, slices, and prints just the part it needs.

Two payloads can't live in a string variable and take a different route
(`broker/capabilities/payload.py`): a **binary** body (PDF, image, zip) and a
**very large** text body both spill to a workspace file, and the capability
returns a `path` + head `preview` instead of inline `text`. The agent then reads
or parses the file with ordinary Python — which is the whole bet: the workspace
is its filesystem, and a saved payload is just data on disk. An explicit
`save="path"` forces the same route. Injected secrets are masked *before* a body
is written, so a spilled file is as safe as an inline read.

## Builtins vs tools

The agent reaches the world the two ways Python itself does, split by one line —
**builtins are the agent's own body; tools are everything it reaches out to:**

- **Builtins** — a small fixed set always in scope, called by bare name: its
  workspace (`read`, `write`, `bash`, `search`), delegation (`llm`, `agent`,
  `map_agents`), reflection (`history`, `save_skill`, `secrets`), and the
  tool-discovery entrypoint (`search_tools`, `describe_tool`, `use_tool`). See
  [Builtins](../reference/builtins.md).
- **Tools** — every external system: web access, a browser, HTTP APIs, the
  package index, MCP servers, learned skills. None are in scope automatically.
  The agent finds one with `search_tools()`, inspects it with `describe_tool()`,
  and loads it with `use_tool()`. So there is exactly one way to reach anything
  external — discover it — and the agent must plan what it needs rather than
  having capabilities handed to it. This two-level discovery also keeps a large
  catalog from flooding the agent's context. A tool call is broker-gated exactly
  as a builtin's is; the two-way split is about surfacing, not enforcement.

## Delegation

Because a cell is just code, the agent can spawn more agents from inside one:
`llm()` for a single completion, `agent()` for a sub-agent that can itself act,
and `map_agents()` for parallel fan-out. Bulk work runs on the cheap tier and
never fills the orchestrator's own context — only the summarized results return.

Everything a cell does still routes through [the broker](broker.md), so this
freedom is bounded by policy, audit, and budget.
