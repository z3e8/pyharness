# The broker

*Understanding-oriented: why every side effect goes through one place.*

Files, shell, web, LLM calls, sub-agents, and tools all route through a single
dispatch (`pyharness/broker/dispatch.py`) that runs **policy → audit → budget →
execute**. In-process today; swappable for an isolated child process later.

<!-- TODO: why a single choke point (uniform policy/audit/budget, one seam to
sandbox), and what the isolation boundary buys. -->
