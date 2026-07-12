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
  a variable and never cost context tokens.
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
