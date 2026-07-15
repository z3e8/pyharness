# pyharness

An AI agent whose **action space is Python**. The orchestrator does exactly two
things: reply with text, or emit one `run_python` call the harness executes in a
persistent kernel. There are no fine-grained JSON tool calls — when the agent
needs a capability, it writes Python (`read(path)`, `bash(cmd)`,
`map_agents(tasks)`).

See the [documentation](docs/index.md) for the full design, guides, and reference.

## How it works

- **Session = a Jupyter kernel.** Each `run_python` is a cell; variables persist
  across cells. Only what the agent `print()`s returns to its context, so large
  data lives in variables, unseen.
- **One broker, every side effect.** Files, shell, web, LLM calls, sub-agents,
  and tools all route through a single dispatch that does policy → audit →
  budget → execute. In-process today; swappable for an isolated child later.
- **Delegation.** `llm()`, `agent()`, and `map_agents()` let the orchestrator
  fan out bulk work to cheaper models without filling its own context.

## Usage

```python
from pyharness import Session, Budget

session = Session(".sessions/demo", budget=Budget(limit_usd=2.0))
print(session.run("Write fib.py, run it, and confirm the output."))
```

The agent reaches the world the way Python does — **builtins** always in scope
are the agent's own body (called by bare name: `read`, `bash`, `llm`, `agent`,
`search_tools`, …); **tools** are everything it reaches out to — web access, a
browser, HTTP APIs, a read-only email inbox, the package index, MCP servers,
learned skills — none in
scope by default, each found with `search_tools()` and loaded with `use_tool()`.
The [Builtins reference](docs/reference/builtins.md) lists the full set. Relative
paths resolve inside the session workspace.

**Skills.** A skill is a learned tool the agent (or a human) saves once and reuses
across sessions: markdown instructions plus optional bundled `.py` modules, stored
under `~/.pyharness/skills/<name>/` (override with `Session(skills_dir=...)`). The
agent writes one with `save_skill(name, description, instructions, files=...)`;
`describe_tool` shows its instructions, `use_tool` loads its code. See
[Add a tool or save a skill](docs/how-to/add-a-tool-or-skill.md).

## Run

Everything is driven by `make` (run `make help` for the full list); config lives
in one `.env`:

```bash
make setup     # create .env + install (once); then set ANTHROPIC_API_KEY in .env
make dev       # observability (Phoenix) + the agent, one command → http://localhost:6006
make run       # just the agent (no observability)
make test      # tests (no API key)
```

Or drive it directly without make:

```bash
uv venv && uv pip install -e .
ANTHROPIC_API_KEY=... uv run python examples/demo.py   # one task
ANTHROPIC_API_KEY=... uv run pyharness                  # interactive CLI

uv pip install pytest && uv run pytest -q               # tests (no API key)
```

Observability is opt-in — see [Run with observability](docs/how-to/observability.md).
