# pyharness

`pyharness` is a tiny loop for AI agents that can do exactly two things:

- Reply with plain text.
- Reply with one fenced `python` block, which the harness runs in a persistent
  namespace.

The captured output from Python code is fed back to the model. Mixed text and
code is rejected, so the loop stays predictable.

## Usage

```python
from pyharness import Agent, AnthropicLLM, Workspace

workspace = Workspace(".sessions/demo")
agent = Agent(AnthropicLLM(), workspace)

answer = agent.run("Write and run a Python script that prints hello.")
print(answer)
```

Agent-written Python receives these helpers:

- `bash(cmd, timeout=60)`
- `read(path)`
- `write(path, content)`
- `edit(path, old, new)`
- `search(pattern, path=".")`

Relative paths resolve inside the workspace.

## Run

```bash
uv venv && uv pip install -e '.[anthropic]'
uv run python examples/demo.py    # needs ANTHROPIC_API_KEY

uv pip install pytest && uv run pytest -q
```
