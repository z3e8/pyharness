# Suite D — the skill cost curve

**Produced 2026-07-31** by `python -m evals.skills.run` against haiku-4.5, $0.0993
over five runs (plus a $0.0200 single-run probe used to calibrate the budget cap).
Refreshing it costs real model calls, so it is committed as a dated artifact
rather than regenerated in CI; the offline suite (`make test`,
`evals/test_skills.py`) is what keeps the machinery under it from rotting
silently.

## The claim under test, and the answer

**Claim:** the agent saves a skill on run 1 and reuses it on runs 2-5, so cost,
latency and steps per run should fall and then flatten.

**Result: they did not.** Runs 2-5 cost **+40%** more than run 1 on average, took
**+19%** more steps and **+15%** more wall time. Every run was correct and every
run used the skill — this is not a story about the mechanism failing to engage.
The mechanism engaged fully and bought nothing.

| | run 1 | run 2 | run 3 | run 4 | run 5 |
|---|---|---|---|---|---|
| cost | $0.0150 | $0.0184 | $0.0218 | $0.0248 | $0.0193 |
| steps | 8 | 8 | 11 | 12 | 7 |
| wall | 16.7s | 17.1s | 20.1s | 21.0s | 18.4s |
| skill | saved | reused | **re-saved** + reused | reused | reused |

n=5, one task, one model. That is enough to say *this task did not amortize*, and
nowhere near enough to say skills do not amortize. It is published because the
plan asked for the answer either way.

## Why — what the record shows

Two things in the audit chain explain the shape, and both are about this task
rather than about haiku:

- **The work is fetching, not figuring out.** Every one of the five runs made
  exactly **2 successful `web.fetch` calls** — the invoice and the remittance
  page. That is the floor, and the skill cannot lower it: a saved procedure
  removes the cost of *discovering* the two URLs, and discovery was already only
  a fraction of a run. Output tokens, which dominate the spend at this size, were
  1230 on run 1 and 1277-1781 afterwards.
- **Reuse has its own overhead, paid every run.** Run 1 spent its skill budget on
  `search_tools` ×2 → `save_skill` → `record_skill_use`. Runs 2-5 spent theirs on
  `search_tools` → `describe_tool` (reading the procedure back into context) →
  `record_skill_use`, and run 4 added an `obs.stats` query and a second
  `describe_tool`. The reuse path is not shorter than the authoring path here; it
  is a different set of calls of about the same size.

And one thing about the skill itself, which is the most actionable finding:

- **The agent saved prose, not code.** `save_skill` takes a `files=` argument that
  bundles importable `.py` modules; the model used none of it. The saved
  `SKILL.md` is a good runbook — both URLs, where the fields sit, the output
  format — but following it still means writing the fetch and the parse from
  scratch on every run. A skill that bundled a `summarize()` function would have
  collapsed runs 2-5 to roughly one cell. Whether the agent can be led to bundle
  code is an open question, logged in `agents/issues.md`; it was not changed here,
  because changing the harness to make the number better is how a suite starts
  lying.

## What this does not say

- **Not a fact about skills in general.** One task, and one deliberately chosen
  to be a *procedure* (two hops, arithmetic, a fixed output shape). A task whose
  expensive part is discovery — an API whose auth flow takes six tries to work
  out — is exactly where the amortization would show, and is not measured here.
- **Not a fact about model choice.** haiku-4.5 at `tier="cheap"`, five samples,
  no repeats at another tier.
- **Not a clean cost measurement.** Prompt caching is live and the trace records
  only *uncached* input tokens — `Usage` carries `cache_read_tokens` and
  `cache_creation_tokens` but they are dropped before the trace, so per-run cost
  cannot be split into prompt/completion/cache from the published record (logged
  in `agents/issues.md`). Runs 1, 2 and 5 recorded ~50 uncached input tokens
  against 5,205 and 7,650 for runs 3 and 4, which is a cache-behaviour difference
  showing up directly in the cost column.
- **Run 1 is not a clean baseline for "no skill".** It includes authoring the
  skill, and the preamble of every later run also carries a growing list of recent
  sessions. Runs 2-5 differ from run 1 in more than one way.
- **Reuse was instructed, not spontaneous.** The task prompt tells the agent to
  look for a saved skill and to save one if there is none. That is deliberate: the
  question is whether reuse is cheaper, and leaving it implicit would risk a flat
  curve produced by a model that simply never saved anything — which would look
  identical while having measured nothing.

## Honesty note on the scorer

Every run is checked for two markers that can only come from the two pages, one of
which (the account number) appears solely on the second hop, plus proof it
re-fetched the source. **That check was changed after the paid run.** It
originally required the literal `1240.00`, and three runs reported the line-item
sum as the Python float `1240.0`; all three had fetched both pages and had the
arithmetic right, so the check was failing them for decimal places. It now matches
the amount with a pattern. The change reclassified two runs from "not usable" to
"usable" and **changed no number in the table above** — cost, steps and latency
come from the session index and were never touched. The board was re-derived from
the same five session dirs with `--rescore`, not by running the model again.

## The board

Regenerate from the sessions with
`python -m evals.skills.run --rescore .sessions/skills-20260731`
(`.sessions/` is gitignored, so this only works on the machine that ran it).

```

  skill cost curve — 5 runs of one task ($0.0993)

    run  outcome        cost      steps  llm  wall     skill / answer
    1    answered       $0.0150   8      9      16.7s  saved northwind_billing_summary, recorded northwind_billing_summary:worked
                                                      ok, 2 fetches
    2    answered       $0.0184   8      9      17.1s  reused northwind_billing_summary, recorded northwind_billing_summary:worked
                                                      ok, 2 fetches
    3    answered       $0.0218   11     12     20.1s  saved northwind_billing_summary, reused northwind_billing_summary, recorded northwind_billing_summary:worked
                                                      ok, 2 fetches
    4    answered       $0.0248   12     13     21.0s  reused northwind_billing_summary, recorded northwind_billing_summary:worked
                                                      ok, 2 fetches
    5    answered       $0.0193   7      8      18.4s  reused northwind_billing_summary, recorded northwind_billing_summary:worked
                                                      ok, 2 fetches

    run 1        $0.0150, 8 steps, 16.7s
    runs 2-5      $0.0211 mean, 9.5 steps, 19.1s
    delta        cost +40%, steps +19%, wall +15%

    skill_run_costs (the view obs/index.py ships):
      northwind_billing_summary  run 1  $0.0150  answered
      northwind_billing_summary  run 2  $0.0184  answered
      northwind_billing_summary  run 3  $0.0218  answered
      northwind_billing_summary  run 4  $0.0248  answered
      northwind_billing_summary  run 5  $0.0193  answered

```

The bottom block is `skill_run_costs` read verbatim out of the session index —
the same view the agent itself reaches through `stats()`. The suite reports it
rather than computing its own per-run cost, so what is published here is the
number the harness already knows how to answer with.
