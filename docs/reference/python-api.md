# Python API

*Precise reference for the public library surface.*

```python
from pyharness import Session, Budget
```

- `Session(workspace, budget=..., skills_dir=...)` — a session backed by a
  persistent kernel; `.run(task)` executes a task and returns the final text.
- `Budget(limit_usd=...)` — spend cap enforced by the broker.

<!-- TODO: full constructor params, methods, and return types. Source of truth:
pyharness/__init__.py and pyharness/core/session.py. -->
