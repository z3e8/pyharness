# pyharness documentation

An AI agent whose **action space is Python**. It either replies with text or
emits one `run_python` call the harness executes in a persistent kernel.

These docs follow [Diátaxis](https://diataxis.fr/) — four sections, each with a
distinct job. Pick by what you're trying to do:

- **[Tutorials](tutorials/)** — learning-oriented. Start here if you're new.
  - [Your first session](tutorials/first-session.md)
- **[How-to guides](how-to/)** — task-oriented recipes for a specific goal.
  - [Run with observability](how-to/observability.md)
  - [Add a tool or save a skill](how-to/add-a-tool-or-skill.md)
  - [Use the secrets vault](how-to/use-the-vault.md)
  - [Deploy](how-to/deploy.md)
- **[Reference](reference/)** — information-oriented lookup.
  - [Builtins](reference/builtins.md) · [CLI](reference/cli.md) ·
    [Configuration](reference/configuration.md) · [Python API](reference/python-api.md)
- **[Explanation](explanation/)** — understanding-oriented background.
  - [The `run_python` action space](explanation/action-space.md) ·
    [The broker](explanation/broker.md) ·
    [Security & audit](explanation/security-and-audit.md) ·
    [Budget](explanation/budget.md)

> Writing docs? Keep each page in its lane: tutorials teach a beginner by doing,
> how-tos solve one real task, reference describes the machinery precisely,
> explanation discusses the why. Don't mix the four.
