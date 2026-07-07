# Budget

*Understanding-oriented: how spend is bounded.*

A `Budget(limit_usd=...)` is enforced by the broker as part of every dispatch, so
the cap holds across the orchestrator and any delegated `llm()`/`agent()`/
`map_agents()` calls.

<!-- TODO: how cost is accounted per call, what happens at the limit, and how
delegation to cheaper models interacts with the cap. Source: pyharness/budget.py. -->
