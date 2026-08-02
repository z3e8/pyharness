# pyharness documentation

An AI agent whose **action space is Python**. It either replies with text or
emits one `run_python` call the harness executes in a persistent kernel.

New here? The [README](../README.md) has the quickstart. These docs cover the
design and the details, in three sections — pick by what you're trying to do:

- **[Explanation](explanation/)** — how it works and why.
  - [The `run_python` action space](explanation/action-space.md) ·
    [The broker](explanation/broker.md) ·
    [Security & audit](explanation/security-and-audit.md) ·
    [Threat model](explanation/threat-model.md) ·
    [Design decisions](explanation/design-decisions.md) ·
    [Budget](explanation/budget.md)
- **[How-to guides](how-to/)** — task-oriented recipes for a specific goal.
  - [Add a tool or save a skill](how-to/add-a-tool-or-skill.md) ·
    [Use the secrets vault](how-to/use-the-vault.md) ·
    [Keep the agent logged in](how-to/site-profiles.md) ·
    [Run with observability](how-to/observability.md) ·
    [Run the adversarial suite](how-to/run-the-adversarial-suite.md) ·
    [Run in Docker](how-to/run-in-docker.md)
- **[Reference](reference/)** — precise lookup for the machinery.
  - [Builtins](reference/builtins.md) · [CLI](reference/cli.md) ·
    [Configuration](reference/configuration.md) · [Python API](reference/python-api.md)

> Writing docs? Keep each page in its lane: how-tos solve one real task,
> reference describes the machinery precisely, explanation discusses the why.
> Don't mix them, and cut a page rather than let it rot. See the `docs` skill.
