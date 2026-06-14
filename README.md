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
`map_agents` `search_tools` `use_tool`. Everything else — installed integrations,
MCP servers, learned skills — is a tool the agent finds with `search_tools()`
and loads with `use_tool()`. Relative paths resolve inside the session workspace.

## Run

```bash
uv venv && uv pip install -e .
ANTHROPIC_API_KEY=... uv run python examples/demo.py   # one task
ANTHROPIC_API_KEY=... uv run pyharness                  # interactive CLI

uv pip install pytest && uv run pytest -q               # tests (no API key)
```
