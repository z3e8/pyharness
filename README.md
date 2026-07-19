# pyharness

<!-- Badge/link targets track the current github.com/z3e8/pyharness origin; they
     move if the repo or the (parked) PyPI package name changes. -->
[![CI](https://github.com/z3e8/pyharness/actions/workflows/test.yml/badge.svg)](https://github.com/z3e8/pyharness/actions/workflows/test.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

An AI agent whose **action space is Python**. The orchestrator does exactly two
things: reply with text, or emit one `run_python` call the harness executes in a
persistent kernel. There are no fine-grained JSON tool calls — when the agent
needs a capability, it writes Python (`read(path)`, `bash(cmd)`,
`map_llm(prompts)`).

See the [documentation](docs/index.md) for the full design, guides, and reference.

## How it works

- **Session = a Jupyter kernel.** Each `run_python` is a cell; variables persist
  across cells. Only what the agent `print()`s returns to its context, so large
  data lives in variables, unseen.
- **One broker, every side effect.** Files, shell, web, LLM calls, sub-agents,
  and tools all route through a single dispatch that does policy → audit →
  budget → execute. Agent code runs in an isolated, OS-sandboxed child process
  by default; an in-process mode exists for tests only.
- **Delegation.** `llm()` and `map_llm()` let the orchestrator fan out bulk
  work to cheaper models without filling its own context.

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
make run       # the agent + its live viewer → http://localhost:6061 (make dev is an alias)
make watch     # live viewer alone, for a session started elsewhere
make test      # tests (no API key)
make lint      # ruff check + format (make format applies fixes; make typecheck runs mypy)
```

Or drive it directly without make. From a clone of this repo:

```bash
uv venv && uv pip install -e . --group dev             # runtime + dev toolchain
ANTHROPIC_API_KEY=... uv run pyharness                  # interactive CLI

uv run pytest -q                                        # tests (no API key)
```

There is no published PyPI package yet — the name is being finalized before the
first release, so install from source (the clone above, or
`uv pip install "git+https://github.com/z3e8/pyharness"` for a runtime-only
install). A `pip install` / `uv pip install` from PyPI will be documented here
once the package is published.

The live viewer is on by default; the heavier OTel export (Phoenix/Langfuse) is
opt-in — see [Run with observability](docs/how-to/observability.md).

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the
`make test` / `make lint` expectations, and the docs-sync convention, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards.

Found a security issue? **Do not open a public issue** — follow
[SECURITY.md](SECURITY.md) (private GitHub advisory).

## License

Apache-2.0 — see [LICENSE](LICENSE).
