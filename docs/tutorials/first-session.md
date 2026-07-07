# Your first session

*Learning-oriented: by the end you'll have run a task end-to-end and understood
what happened.*

## Before you start

- `make setup`, then set `ANTHROPIC_API_KEY` in `.env`.

## Run a task

```python
from pyharness import Session, Budget

session = Session(".sessions/demo", budget=Budget(limit_usd=2.0))
print(session.run("Write fib.py, run it, and confirm the output."))
```

## What just happened

<!-- TODO: walk through the loop — the agent emitted a run_python call, the
kernel executed it, only what it printed came back into context. Point at
docs/explanation/action-space.md for the model. -->

## Next steps

- [Run with observability](../how-to/observability.md) to watch the loop live.
- [The `run_python` action space](../explanation/action-space.md) for the why.
