# Suite D — the skill cost curve

Two arms of one experiment: the same save-then-reuse protocol (five runs of one
task over one shared skills root, haiku-4.5 at `tier="cheap"`) pointed at two
task shapes. The **retrieval** arm ran 2026-07-31 ($0.0993 over five runs, plus
a $0.0200 calibration probe); the **discovery** arm ran later the same day
($0.1577 over five runs, plus a $0.1949 set scrapped for a fixture bug —
disclosed below). Refreshing either costs real model calls, so this is a dated
artifact rather than a CI product; the offline suite (`make test`,
`evals/test_skills.py`) keeps the machinery under it from rotting silently.

## The claim under test, and the answers

**Claim:** the agent saves a skill on run 1 and reuses it on runs 2-5, so cost,
latency and steps per run should fall and then flatten.

**Retrieval arm — no.** Runs 2-5 cost **+40%** more than run 1, took +19% more
steps and +15% more wall time. Every run was correct and used the skill.

**Discovery arm — the curve bends, and something breaks it.** The two reuse
runs that executed the frozen sequence collapsed to 2 fetches; the cheapest
(run 5) finished in 5 steps at **-51%** of run 1's cost. But every reuse run
first paid for a real defect the arm surfaced — the bundled code the agent
saved fails on import (below) — so one run re-walked the whole site and one
died at the step wall with the answer in hand. The mean says +17%; the
distribution says amortization is real here and an identifiable bug is eating
it.

**The boundary condition, stated as the finding:** a skill amortizes when the
task's cost is *discovery* (working out which pages, in which order, with what
transformation) and the reuse path is cheap to execute. It does not amortize
when the cost is *retrieval* (the fetches themselves), because a procedure
cannot lower the fetch floor — and the reuse path has its own per-run overhead
(`search_tools` → `describe_tool`/`use_tool` → `record_skill_use`) that must be
paid out of whatever discovery it removed. On the retrieval task that overhead
exceeded the discovery saved. On the discovery task the saved discovery is ~3
fetches plus the reasoning between them, which covers the overhead — when
executing the skill does not itself misfire.

Two arms on one loopback corpus, one model, one harness, n=5 each. That is
enough to place the boundary for this harness on this corpus, and nowhere near
enough to say anything about "skills" in the abstract.

## Arm 1 — retrieval (two known URLs)

The task: produce a billing summary from an invoice page and the remittance
page it links. Both URLs are effectively given; the work is fetching and
arithmetic.

| | run 1 | run 2 | run 3 | run 4 | run 5 |
|---|---|---|---|---|---|
| cost | $0.0150 | $0.0184 | $0.0218 | $0.0248 | $0.0193 |
| steps | 8 | 8 | 11 | 12 | 7 |
| wall | 16.7s | 17.1s | 20.1s | 21.0s | 18.4s |
| skill | saved | reused | **re-saved** + reused | reused | reused |

n=5: run 1 vs the runs 2-5 mean is **+40% cost, +19% steps, +15% wall**. All
five answered correctly and recorded a use; the mechanism engaged fully and
bought nothing. Two reasons, both in the audit chain:

- **The work is fetching, not figuring out.** Every run made exactly 2
  successful `web.fetch` calls — the floor, which no procedure can lower.
  Discovery (knowing the two URLs) was already a fraction of a run.
- **Reuse has its own overhead, paid every run.** Run 1 spent its skill budget
  on `search_tools` ×2 → `save_skill` → `record_skill_use`; runs 2-5 spent
  theirs on `search_tools` → `describe_tool` → `record_skill_use`. The reuse
  path is not shorter than the authoring path here, just different.

**The agent saved prose, not code.** This arm ran before the lazy-bundled-code
change and its system-prompt nudge landed; `save_skill(files=…)` went unused in
all five runs, so following the runbook still meant re-writing the fetch and
the parse every time. The discovery arm, run after that change, is the direct
test of whether the nudge works — see below.

## Arm 2 — discovery (a sequence the prompt does not reveal)

The task: report the current balance of a supplier-portal account, given only
the portal home page. The balance is deliberately not where it looks like it
should be. The intended walk: home → *Invoices & billing* (the plausible wrong
turn — a note says balances moved and points at the help centre) → the help
article, which gives the statement-address **scheme**
(`statement-<code>.html`, code lower-cased, hyphen dropped) → the profile page,
which holds the code (`RT-1180`) → the assembled terminal URL. The balance and
a confirmation code exist only on that terminal page, no page links it
directly, and the scorer additionally requires the run's audit chain to show a
successful fetch of it — so a run cannot shortcut the sequence, and a later run
cannot replay the markers out of the skill's own text. This is the shape of the
task skills were designed for: everything expensive about run 1 is knowledge a
later run could reuse.

Produced by `python -m evals.skills.run --arm discovery`, five runs, one shared
skills root, $0.1577:

| | run 1 | run 2 | run 3 | run 4 | run 5 |
|---|---|---|---|---|---|
| outcome | answered | **stopped:max_steps** | answered | answered | answered |
| cost | $0.0278 | $0.0427 | $0.0313 | $0.0423 | $0.0136 |
| steps | 11 | 18 | 12 | 16 | 5 |
| wall | 26.5s | 25.8s | 21.4s | 25.1s | 13.2s |
| corpus fetches | 5 | — | 2 | 5 | 2 |
| skill | saved + recorded | reused + recorded | reused + recorded | reused + recorded | reused + recorded |

Headline mean (runs 2-5 vs run 1, the failed run included): cost +17%, steps
+16%, wall -19%. The per-run record is what matters:

- **Run 1 walked the site and saved real code.** Five fetches (home, billing,
  help, profile, statement), then `save_skill(files={"northwind_balance.py":
  …})` bundling a complete `get_northwind_balance()` — profile fetch, regex for
  the code, URL construction, statement fetch, extraction of all three fields.
  **This is the direct positive on the `files=` change:** the retrieval arm's
  agent saved prose five runs out of five; this arm's agent bundled code on its
  first authoring run, unprompted by the task (the tasks are pinned by test to
  never mention `files=` or bundling).
- **Every reuse run hit the same defect: the bundled code fails on import.**
  The model wrote `from pyharness import use_tool` inside the module;
  `use_tool` is a kernel builtin, not a package export, so the first call
  raises `ImportError`. All four reuse runs paid it (twice, in three of them).
  There is currently no supported way for bundled code to reach a capability,
  while the guidance ("a fetch … belongs in `files`") steers the agent into
  writing exactly this. Logged in `agents/issues.md`, not fixed here.
- **Where reuse recovered cheaply, the curve bent.** Runs 3 and 5 transcribed
  the surfaced source (`describe_tool` prints bundled code precisely so a
  reader can see it) into a cell and ran it: 2 fetches each — the frozen
  sequence, against run 1's five. Run 5 did the whole task in 5 steps and
  $0.0136, **51% below run 1**. That is the amortization the retrieval arm
  could not show.
- **Where it recovered expensively, the curve broke.** Run 4 gave up on the
  skill after the ImportError and re-walked the site (5 fetches, 16 steps).
  Run 2 burned four steps on unechoed `search_tools` calls, hit the
  ImportError twice, re-walked the site, found the answer, recorded
  `worked` — and hit the step wall before ever emitting the final line. Its
  cost is in the table; its number is marked unusable.

The counterfactual is visible in the record rather than speculative: run 5 is
what every reuse run looks like when executing the skill costs almost nothing.
A working bundled-code path would make run 5 the mode, not the tail.

## Changed after a paid run — full disclosure

Nothing in either arm was tuned after seeing its published numbers. Two things
were changed after *a* paid run, and both are disclosed here:

- **Arm 1's scorer, after its run.** The check originally required the literal
  `1240.00`; three runs reported the Python float `1240.0` having fetched both
  pages and summed correctly, so the literal was failing them on decimal
  places. It now matches a pattern. The change reclassified two runs from "not
  usable" to "usable" and changed no cost, step or latency number; the board
  was re-derived from the same five session dirs with `--rescore`, not re-run.
- **The discovery arm's terminal page, after a scrapped first set.** The first
  paid set (five runs, $0.1949) is not published: the statement page's balance
  sat in a table that trafilatura — the reduction `web.fetch` actually applies
  — classified as boilerplate and dropped, so **no run could see the answer at
  any price**. All five runs failed (four at a budget/step wall, one answering
  with a number from the wrong page). The page was restructured to the
  invoice's proven shape, an offline test now pins every hop of the chain
  through the real extractor (`test_every_hop_of_the_discovery_chain_survives_
  extraction`), and the published set ran with no further changes. This was
  fixing a broken fixture, not tuning a live one — the scrapped set's runs
  could not reach the thing being measured — but it is a post-run change to the
  task surface and is reported as one. Its cost is included in the totals.

## What this does not say

- **Not a fact about skills in general.** One corpus, one harness, two task
  shapes, and the discovery arm's reuse numbers are dominated by one
  identifiable bug. n=5 per arm supports a shape, not a statistic — the
  discovery arm's later-run costs span $0.0136-$0.0427, which is the variance
  of one model recovering from one error two different ways.
- **Not a fact about model choice.** haiku-4.5 at `tier="cheap"` throughout.
- **Not a clean cost measurement.** Prompt caching is live and the trace
  records only *uncached* input tokens (`cache_read`/`cache_creation` are
  dropped before the trace — logged in `agents/issues.md`), so per-run cost
  cannot be decomposed from the published record.
- **Run 1 is not a clean no-skill baseline.** It includes authoring the skill;
  later runs carry a growing recent-sessions preamble. Runs 2-5 differ from
  run 1 in more than one way.
- **Reuse was instructed, not spontaneous.** Both tasks tell the agent to look
  for a saved skill and save one if absent — deliberately, and pinned by test:
  the question is whether reuse is *cheaper*, and leaving it implicit risks a
  flat curve produced by a model that never saved anything. Equally pinned:
  neither task mentions `files=` or bundling, so the code-bundling observed in
  the discovery arm is attributable to the system prompt, not the task.
- **Bundling is not yet reliable.** Across the two post-change authoring runs
  (the scrapped set's and the published set's), one bundled code and one saved
  prose only — though the prose-only author had never seen the full procedure
  work, since the scrapped set's terminal page was unreadable.

## The boards

Regenerate from the sessions with `python -m evals.skills.run --rescore
.sessions/<root>` (`.sessions/` is gitignored, so this only works on the
machine that ran them: retrieval `skills-20260731`, discovery
`skills-20260731`).

Retrieval arm, verbatim from the run:

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

Discovery arm, verbatim from the run:

```

  skill cost curve — discovery arm, 5 runs of one task ($0.1577)

    run  outcome        cost      steps  llm  wall     skill / answer
    1    answered       $0.0278   11     12     26.5s  saved northwind_portal_balance, recorded northwind_portal_balance:worked
                                                      ok, 5 fetches
   !2    stopped:max_steps $0.0427   18     18     25.8s  reused northwind_portal_balance, recorded northwind_portal_balance:worked
                                                      stopped:max_steps; markers missing
    3    answered       $0.0313   12     13     21.4s  reused northwind_portal_balance, recorded northwind_portal_balance:worked
                                                      ok, 2 fetches
    4    answered       $0.0423   16     17     25.1s  reused northwind_portal_balance, recorded northwind_portal_balance:worked
                                                      ok, 5 fetches
    5    answered       $0.0136   5      6      13.2s  reused northwind_portal_balance, recorded northwind_portal_balance:worked
                                                      ok, 2 fetches

    run 1        $0.0278, 11 steps, 26.5s
    runs 2-5      $0.0325 mean, 12.8 steps, 21.4s
    delta        cost +17%, steps +16%, wall -19%

    skill_run_costs (the view obs/index.py ships):
      northwind_portal_balance  run 1  $0.0278  answered
      northwind_portal_balance  run 2  $0.0427  stopped:max_steps
      northwind_portal_balance  run 3  $0.0313  answered
      northwind_portal_balance  run 4  $0.0423  answered
      northwind_portal_balance  run 5  $0.0136  answered

  runs whose number is not usable:
    run 2: stopped:max_steps; markers missing

```

The `skill_run_costs` block in each board is read verbatim out of the session
index — the same view the agent itself reaches through `stats()`. The suite
reports it rather than computing its own per-run cost, so what is published
here is the number the harness already knows how to answer with.
