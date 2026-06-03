# pyharness

A minimal harness where an agent acts **exclusively by writing Python code**.

The core loop has one move: the model writes a `python` block, the harness runs
it in a persistent namespace, and the captured output is fed back. When the
model replies with no code block, that text is the final answer. Tools, nested
LLM calls, sub-agents, and self-modification are all just Python the agent
writes — not special harness machinery.

## Core components

| Module | Role |
|---|---|
| `agent.py` (`Master`) | the loop: LLM → run code → feed back → repeat |
| `llm.py` | pluggable `LLMProvider`; `AnthropicProvider` (fast/smart tiers) + `FakeProvider` |
| `session.py` | a folder on disk; the agent's workspace |
| `permissions.py` (`RulePolicy`) | fine-grained allow/deny over `action:resource` keys |
| `tools.py` (`Toolbox`) | the capability API injected into agent code, every call gated |
| `executor.py` | runs the code, captures stdout/stderr/tracebacks |

The agent's namespace exposes: `bash`, `read`, `write`, `edit`, `search`,
`http_get`, `http_post`, `llm`, `Tier`, `session`.

## Permissions

Every capability call routes through `RulePolicy.check(action, resource)` — the
single chokepoint. Denials raise `PermissionDenied`, which surfaces back to the
agent as feedback so it can adapt.

```python
RulePolicy(allow=["bash:*", "file.write:/work/*"], deny=["bash:rm *"])
```

> **Note:** code runs in-process, so the policy mediates the *provided*
> capabilities — it is not a hard OS sandbox. Process/container isolation is a
> clean later upgrade since capabilities are already a single boundary.

## Run

```bash
uv venv && uv pip install -e '.[anthropic]'
uv run python examples/demo.py    # needs ANTHROPIC_API_KEY

uv pip install pytest && uv run pytest -q
```
