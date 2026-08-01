# Static session pages

The ten demo sessions from `.sessions/demo-20260730-225820`, each baked into one
self-contained HTML file, plus an `index.html` that lists them. Open
`index.html` in a browser — no server, no build step, no network.

These are the **same viewer** the operator watches live (`make watch`), fed a
recorded trace instead of an SSE stream: `obs/watch.py` owns the renderer,
`obs/static.py` swaps the feed. There is no second implementation to drift.

## Why these are committed

`.sessions/` is gitignored, so the traces behind these pages are not in the
repo. Regenerating them needs the original run — which cost real money against a
real API. The pages are therefore the artifact, the way `evals/SCOREBOARD.md`
and `../COMPARISON.md` are: committed output, not committed input.

`make site` rebuilds them from a local run (`SITE_RUN=` to point at another).

## What to look at

`../COMPARISON.md` is the board; these are the sessions behind its rows.

| Page | What it shows |
|------|---------------|
| `release-samehost.html` | The headline. An ordinary operator instruction to release a vault credential, refused — with the human's approval already **granted**. Whatever stopped it was the host scope, not the gate. |
| `release-approved.html` | The contrast. Same instruction, same credential, different destination: it goes through, the credential is attached, and no cleartext appears in the record. |
| `invoice-hostile-samehost.html` | A CSS-hidden injected payload reaching the model, and the model declining it. Reported as a fact about the model, with its own denominator. |
| `endurance-budget.html` | A run walking into a budget wall. |

## Not published

Sub-agent fan-out. The plan wanted one page showing scoped children, but no
session in this run spawns — every demo task is single-agent. A page for it
would have to come from a different run.

## Before this is ever public

The pages redact the operator's home directory (see `obs/static.py`), but
publishing at all is gated on the repo going public, which is an open decision —
see `agents/README.md` § "Deferred: open-source release".
