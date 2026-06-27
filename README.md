# pyharness

An AI agent whose **action space is Python**. The orchestrator does exactly two
things: reply with text, or emit one `run_python` call the harness executes in a
persistent kernel. There are no fine-grained JSON tool calls — when the agent
needs a capability, it writes Python (`web_search(q)`, `read(path)`,
`map_agents(tasks)`).

See [`docs/design.md`](docs/design.md) for the full design and the V1-vs-later split.

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

The agent reaches the world the way Python does — **builtins** always in scope,
**tools** imported on demand. Builtins (called directly by bare name): `read`
`write` `edit` `bash` `search` `web_search` `web_fetch` `llm` `agent`
`map_agents` `search_tools` `use_tool` `save_skill`. Everything else — installed
integrations, MCP servers, learned skills — is a tool the agent finds with
`search_tools()` and loads with `use_tool()`. Relative paths resolve inside the
session workspace.

**Skills.** A skill is a learned tool the agent (or a human) saves once and reuses
across sessions: markdown instructions plus optional bundled `.py` modules, stored
under `~/.pyharness/skills/<name>/` (override with `Session(skills_dir=...)`). The
agent writes one with `save_skill(name, description, instructions, files=...)`;
`describe_tool` shows its instructions, `use_tool` loads its code. See
[`docs/skills.md`](docs/skills.md).

## Run

Everything is driven by `make` (run `make help` for the full list); config lives
in one `.env`:

```bash
make setup     # create .env + install (once); then set ANTHROPIC_API_KEY in .env
make run       # interactive CLI
make test      # tests (no API key)

make up        # optional: local observability stack (Langfuse + Prometheus)
make observe   # optional: built-in file-based session timeline UI
```

Or drive it directly without make:

```bash
uv venv && uv pip install -e .
ANTHROPIC_API_KEY=... uv run python examples/demo.py   # one task
ANTHROPIC_API_KEY=... uv run pyharness                  # interactive CLI

uv pip install pytest && uv run pytest -q               # tests (no API key)
```

Observability is opt-in — see [`docs/observability.md`](docs/observability.md).
