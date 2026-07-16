---
name: docs
description: How to read, write, and maintain the pyharness docs under docs/. Use when adding or editing a docs page, deciding where documentation belongs, syncing docs after a code change, or when you need to consult the docs to understand a subsystem. Covers the three-section layout, the reference↔source map, and the "cut rather than rot" rule.
---

# Maintaining the pyharness docs

The docs exist for one audience: **you and the agents working in this repo.**
Not a public product, not newcomers who aren't here. Every page must earn its
keep as non-inferable knowledge someone will actually reach for. When in doubt,
cut it — a missing page costs a grep; a stale page lies.

## Read narrowly, not broadly

Do **not** bulk-read `docs/` to "get context." Load the single page tied to the
subsystem you're touching:

| Working on… | Read |
|-------------|------|
| the run_python model, kernel, delegation | `explanation/action-space.md` |
| dispatch / capability routing | `explanation/broker.md` |
| policy, vault, sandbox, audit chain | `explanation/security-and-audit.md` |
| budget, tiers, pricing | `explanation/budget.md` |
| the builtin surface | `reference/builtins.md` |
| env vars / config | `reference/configuration.md` |
| the public Python API | `reference/python-api.md` |
| the CLIs | `reference/cli.md` |

The code is ground truth; a doc page is a faster, curated summary. If a page
disagrees with the code, the code wins — and you fix the page (see below).

## The three sections (Diátaxis, minus tutorials)

Each page stays in exactly one lane. Mixing lanes is the main way these rot.

- **explanation/** — *why and how it works.* Architecture and rationale. Most
  durable; changes only when the design changes. New conceptual background goes
  here.
- **how-to/** — *one real task, start to finish.* "Add a tool," "use the vault,"
  "run with observability." If it's not a task someone actually performs, it
  doesn't belong. (This is why there's no `deploy` page — nothing is deployed.)
- **reference/** — *precise lookup.* Tables of signatures, flags, env vars. Lean;
  no narrative. This section drifts fastest — see the sync map.

There is no `tutorials/` section. Onboarding lives in the top-level `README.md`.
Don't recreate a tutorial quadrant.

## Sync docs after a code change (do this in the same commit)

A change is not done until the docs it affects are true again. The reference
pages track specific sources — when you touch the left, update the right:

| If you change… | Update |
|----------------|--------|
| the `SYSTEM_PROMPT` builtins in `pyharness/core/agent.py` | `reference/builtins.md` |
| env vars (add/rename/default) in `.env.example` | `reference/configuration.md` |
| the public exports in `pyharness/__init__.py` | `reference/python-api.md` |
| CLI flags/behavior in `cli/main.py` / `cli/vault.py` | `reference/cli.md` |
| tiers, pricing, or budget enforcement | `explanation/budget.md` |
| policy defaults, vault rules, or the sandbox | `explanation/security-and-audit.md` |
| how side effects route through the broker | `explanation/broker.md` |

Also check `README.md` (it has the quickstart and a few illustrative builtin
names, then links to `reference/builtins.md` for the full set) and the repo map
in `AGENTS.md` when structure moves.

## Writing rules

- **Lean.** Reference is tables, not prose. Link instead of repeating — each
  fact has one home; other pages point to it.
- **Concrete over aspirational.** Document what exists, not what you might build.
  No "in a shared deployment you would…" pages.
- **Real internal links.** Paths are relative to the page's own directory; the
  cross-links between pages are load-bearing, so don't break them when moving
  content.
- **Cut before you pad.** If a page has thinned to a stub that just points
  elsewhere, delete it and repoint its inbound links.

## When adding a genuinely new page

1. Confirm it's non-inferable and someone will reach for it. If not, don't.
2. Pick the one lane it belongs to; if it spans two, it's two pages.
3. Add it to the section list in `docs/index.md`.
4. If it's reference, add its source to the sync map above.
