# Contributing to pyharness

**Read this as fork documentation, not a call for contributions.** pyharness is a
reference implementation under a feature freeze, not a maintained package, so
issues and pull requests may not be reviewed promptly. What follows is how to
build, test and extend it, which is what you need whether you are changing your
own fork or reading to understand how it holds together. If you do open a pull
request anyway, the bar is small, well-scoped changes that keep the tests green
and the docs true.

## Toolchain

pyharness uses [`uv`](https://docs.astral.sh/uv/) as its package manager and
`make` as the single entry point. **Do not use bare `pip`, `venv`, `poetry`, or
`conda`** — everything is driven through `uv` so contributor and CI environments
match exactly. Run `make help` to see every target.

Python 3.11+ is required (the suite runs on 3.11 / 3.12 / 3.13 in CI).

## Set up

```bash
make setup     # creates .env from .env.example and installs the package (editable) + dev toolchain
```

`make setup` runs `uv venv` and `uv pip install -e . --group dev`, which pulls in
the dev tools (pytest, ruff, mypy). No API key is needed to run the tests. You
only need `ANTHROPIC_API_KEY` in `.env` to actually run the agent (`make run`).

## Before you open a PR

Run these locally; CI enforces the first two.

```bash
make test        # pytest — must be green. No API key required.
make lint        # ruff check + ruff format --check — must be clean (this is what CI gates on)
make format      # auto-fix: ruff format + ruff check --fix
make typecheck   # mypy — lenient and NON-blocking (informational; see pyproject [tool.mypy])
```

- **Tests** (`make test`) must pass. Add tests for behavior you change or add.
- **Lint** (`make lint`) must be clean. Run `make format` to auto-fix most issues.
  Line-length (E501) is intentionally not linted — the formatter owns code width.
- **Typecheck** (`make typecheck`) is lenient and does not gate merges. Don't add
  new type errors gratuitously, but you don't have to drive the count to zero.

## Conventions

- **Keep changes surgical.** Touch only what the task needs; match the surrounding
  style. Prefer the durable path over the quick one.
- **One logical change per commit.** If the commit message needs an "and", split
  it. No secrets and no debug noise in commits — never commit `.env` or anything
  under `.sessions/`.
- **All side effects route through the broker.** New agent-visible capabilities go
  in `pyharness/broker/` (see the dispatch choke point in
  `pyharness/broker/dispatch.py`), not scattered I/O across modules.
- **Keep docs true in the same change.** A change that alters behavior, a builtin,
  a flag, or a config var must update the matching page under `docs/` in the same
  commit. The reference pages track specific sources:
  - `docs/reference/builtins.md` ↔ `SYSTEM_PROMPT` in `pyharness/core/agent.py`
  - `docs/reference/configuration.md` ↔ `.env.example`
  - `docs/reference/python-api.md` ↔ `pyharness/__init__.py`

  See `docs/index.md` for the full docs layout (explanation / how-to / reference).
- **The audit log is a tamper-evident hash chain.** Verify one with
  `make verify-audit DIR=.sessions/<name>`.

## Reporting bugs and requesting features

Use the GitHub issue templates. For **security vulnerabilities, do not open a
public issue** — follow [SECURITY.md](SECURITY.md) (private GitHub advisory).

## Dependency licensing note

The runtime dependency `trafilatura` (web-page content extraction) is pinned at
`>=2.1`; version 2.1.0 is Apache-2.0. Historically older 1.x releases were
GPL-3.0+, so if you bump this pin, re-check the license of the target version —
pyharness is Apache-2.0 and adding a copyleft runtime dependency would be a
licensing decision, not a routine upgrade.
