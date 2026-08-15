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
`read(path)`, `bash(cmd)`, `map_llm(prompts)`.

This shape has a name in the literature: **CodeAct**, from Wang et al.,
[*Executable Code Actions Elicit Better LLM Agents*](https://arxiv.org/abs/2402.01030)
(ICML 2024), which consolidates an agent's actions into executable code rather
than a fixed set of tool schemas. That paper argues the pattern is *better* —
more expressive, fewer round trips. This repo takes the pattern as given and
asks the question the paper does not: an action space that can express anything
can also do anything, so what does it take to contain one? Everything from
[the broker](broker.md) down is the answer.

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
- **A familiar action space.** Models are fluent in Python. Writing code is
  closer to how they were trained than filling bespoke tool schemas.

These are claims, and one of them is measured rather than asserted: the
[throughput suite](../how-to/run-the-throughput-suite.md) runs the same task
through this harness, through a conventional `read_file`/`list_files` pair, and
through a shell, over ~50MB of logs that no arm can read into its context. The
board is [`evals/data/BOARD.md`](../../evals/data/BOARD.md). Read it before
trusting the bullets above: a shell arm gets much of the same leverage.

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
  guards the context window, not the data. A separate memory guard sits under
  it: capture itself is bounded at `util.CAPTURE_CEILING` (~1M chars, well above
  the display cap), so a runaway `print` of gigabytes can't OOM the process
  before that display truncation runs.

Keeping these separate is the point: the agent holds the whole thing and decides
how much to surface. It searches, slices, and prints just the part it needs.

Two payloads can't live in a string variable and take a different route
(`broker/capabilities/payload.py`): a **binary** body (PDF, image, zip) and a
**very large** text body both spill to a workspace file, and the capability
returns a `path` + head `preview` instead of inline `text`. The agent then reads
or parses the file with ordinary Python: the workspace is its filesystem, and a
saved payload is just data on disk. An explicit
`save="path"` forces the same route. Injected secrets are masked *before* a body
is written, so a spilled file is as safe as an inline read.

## Builtins vs tools

The agent reaches the world the two ways Python itself does, split by one line —
**builtins are the agent's own body; tools are everything it reaches out to:**

- **Builtins** — a small fixed set always in scope, called by bare name: its
  workspace (`read`, `write`, `edit`, `bash`, `search`), credentials
  (`secrets`), delegation (`llm`, `map_llm`), tool discovery
  (`search_tools`, `describe_tool`, `use_tool`, `add_mcp_server`), skills
  (`save_skill`, `edit_skill`, `record_skill_use`), reflection on its own
  record (`history`, `stats`, `inspect_session`), and reaching the human
  (`notify`). See [Builtins](../reference/builtins.md).
- **Tools** — every external system: web access, a browser, HTTP APIs, the
  package index, MCP servers, learned skills. None are in scope automatically.
  The agent finds one with `search_tools()`, inspects it with `describe_tool()`,
  and loads it with `use_tool()`. So there is exactly one way to reach anything
  external — discover it — and the agent must plan what it needs rather than
  having capabilities handed to it. This two-level discovery also keeps a large
  catalog from flooding the agent's context. A tool call is broker-gated exactly
  as a builtin's is; the two-way split is about surfacing, not enforcement.

## Delegation

Because a cell is just code, the agent can spawn more work from inside one:
`llm()` for a single completion and `map_llm()` for parallel fan-out of the
same call over many prompts. Both are **LLM calls as functions** — one-shot
text workers that cannot themselves call a capability or run code, not nested
run_python loops. Their `context` parameter is the context-hygiene seam: the
agent hands a large kernel variable to a worker (`llm("what changed?",
context=big_var)`) instead of printing it into its own history. Fan-out is
count-capped (`session_cap`/`max_per_call` in
`pyharness/broker/capabilities/llm.py`) and runs on the cheap tier by default;
only the distilled results return to the orchestrator's context.

The second tier is `spawn()` — a **real sub-agent**. A spawned child is a full
recursive `Session`: its own kernel, its own message history, its own step and
budget walls, and a capability allowlist granted at spawn time
(`spawn(tools=...)`), optionally confined to a host scope
(`allowed_hosts=[...]` — see
[security](security-and-audit.md#host-scoped-sessions)). Unlike an `llm()`
worker it can act — fetch, browse, run
code — so gather-work stays out of the orchestrator's context entirely.
Spawning is **asynchronous**: the call returns a handle immediately and the
child runs in a parent-side thread, so the orchestrator can start several
children in one cell, keep working, and `wait()` for the distilled reports
when it needs them (`spawn_status()` is the cheap glance in between). The
composition reuses the same machinery everywhere: the child's side effects
route through its own broker into the *parent's* audit chain, its approvals
bubble to the same human (labeled), its spend settles into the parent's
budget, and its session dir is indexed like any other (so `inspect_session`
answers follow-ups about it). The handoff is deliberately two-channel: a
condensed final report (the child's last message, returned verbatim in a
`SpawnResult`), plus the shared workspace for anything large. Depth is one by
construction — a child's capability set never includes `spawn` — and the
`spawn` call itself always needs human approval: approving it is approving
the child's whole plan (task, capability set, host scope, budget slice).

Every capability a cell calls still routes through [the broker](broker.md), so
this freedom is bounded by policy, audit, and budget; the code between those
calls is bounded by the OS sandbox.

## The per-session venv

`pyharness/core/session_venv.py:SessionVenv` is a lazily-created, per-session
virtualenv that backs the `packages` tool. It is only ever created in
**out-of-process** mode (`RemoteKernel._start` calls `ensure_created()` when
spawning the child; nothing calls it in-process) — so `packages.install()` in
an in-process session returns a message saying it requires out-of-process mode
rather than installing anything. When created, its site-packages are injected
into the child so an installed package is importable in later cells.

## After the session ends: reflection

The run_python loop is not the only place the agent revises itself.
`Session.reflect()` (`pyharness/reflect.py`) runs after `session.messages` is
already final: a cheap-tier LLM call reads the session's trace and may propose
a lesson, an `edit_skill`, or a `save_skill` — routed through the same broker
approval gate as an in-session skill write. Established lessons
(`pyharness/lessons.py` — a fact promoted after it recurs across enough
distinct sessions) feed back into the *next* session's preamble, so this is how
the agent accumulates procedure knowledge across sessions rather than just
within one. It's driven by the trace, not by agent code, and skipped outright
once the session's budget is exhausted. See
[observability](../how-to/observability.md#post-session-reflection).
