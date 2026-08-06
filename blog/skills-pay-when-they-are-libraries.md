# A saved skill pays for itself only when it is a library

Agents that can write down what they learned are supposed to get cheaper.
Run a task once, save a procedure, and every later run reads the procedure
instead of rediscovering the answer. Cost per run falls, then flattens.

I built that into [pyharness](https://github.com/z3e8/pyharness) and then
measured it, because the story is plausible enough that nobody checks. The
measurement says the curve is real, but only for one shape of task, and only
when the saved thing is executable. When the agent saves prose, reuse costs
*more* than the original run.

Two arms, five runs each, one model (haiku-4.5 at the cheap tier), one
loopback corpus. Every number below is from the committed board in
[`evals/skills/CURVE.md`](https://github.com/z3e8/pyharness/blob/main/evals/skills/CURVE.md),
which ships the per-run record so you can check the headline against the
distribution.

## The protocol

Both arms run the same shape: five sequential sessions against one shared
skills directory. The task tells the agent to look for a saved skill and to
save one if none exists. That instruction is deliberate and pinned by a test,
because the question is whether reuse is *cheaper*, and leaving it implicit
risks a flat curve produced by a model that simply never saved anything.

Equally pinned: neither task ever mentions bundling code. `save_skill` takes
a `files=` argument mapping filename to Python source, and whether the agent
uses it is attributable to the system prompt, not to the task text. That turns
out to be the whole result.

## Arm 1: retrieval. The curve goes the wrong way.

The task is a billing summary built from an invoice page and the remittance
page it links. Both URLs are effectively given. The work is two fetches and
some arithmetic.

| | run 1 | run 2 | run 3 | run 4 | run 5 |
|---|---|---|---|---|---|
| cost | $0.0150 | $0.0184 | $0.0218 | $0.0248 | $0.0193 |
| steps | 8 | 8 | 11 | 12 | 7 |
| wall | 16.7s | 17.1s | 20.1s | 21.0s | 18.4s |

Runs 2-5 against run 1: **+40% cost, +19% steps, +15% wall time.** All five
runs answered correctly, all five reused the skill, all five recorded the use.
The mechanism engaged completely and bought nothing.

The audit chain gives two reasons.

**The work was never discovery.** Every run made exactly two successful
`web.fetch` calls. Two is the floor. No procedure lowers it, because the
procedure is not what costs money here. Knowing which two URLs to fetch was
already a rounding error in run 1.

**Reuse has its own overhead, paid every run.** Run 1 spent its skill budget
on `search_tools` twice, then `save_skill`, then `record_skill_use`. Runs 2-5
spent theirs on `search_tools`, `describe_tool`, `record_skill_use`. The reuse
path is not shorter than the authoring path. It is just different. On a task
with almost no discovery to remove, that overhead is pure loss.

And the agent saved prose. `files=` went unused in all five runs, so following
the runbook still meant re-writing the fetch and the parse from scratch every
time. The skill was a document about work, not the work.

## Arm 2: discovery. The library case.

The second task hides the sequence. The agent gets a supplier portal home page
and is asked for the account balance. The balance is not where it looks like it
should be. The intended walk is five pages: home, then *Invoices & billing*
(the plausible wrong turn, which carries a note saying balances moved), then a
help article that gives the statement-address *scheme* (`statement-<code>.html`,
code lower-cased, hyphen dropped), then the profile page holding the code
(`RT-1180`), then the assembled terminal URL.

No page links the terminal page directly. The scorer requires the run's audit
chain to show a successful fetch of it, so a run cannot shortcut the sequence
and a later run cannot replay the answer out of the skill's own text. This is
the shape skills were designed for: everything expensive about run 1 is
knowledge a later run could reuse.

Run 1 walked all five pages and then did the thing the retrieval arm never did.
It called `save_skill(files={"northwind_balance.py": ...})` with a complete
`get_northwind_balance()`: profile fetch, regex for the code, URL construction,
statement fetch, extraction of all three fields. Unprompted by the task. The
skill stopped being a document and became a callable.

Runs 3 and 5 called it and collapsed to **two fetches** against run 1's five.
Run 5 finished the whole task in 5 steps for $0.0136, **51% below run 1**.

That is the amortization the retrieval arm could not produce, and the mechanism
is specific: a frozen sequence executes in one call instead of being
reconstructed by a model reading instructions.

## The bug the arm found

Runs 2 and 4 did not collapse. Every reuse run hit the same defect first: the
bundled code failed on import. The model had written `from pyharness import
use_tool` inside the module, but `use_tool` is a kernel builtin, not a package
export. Four runs out of four paid an `ImportError`, twice in three of them.
Run 4 gave up and re-walked the site. Run 2 burned steps, re-walked, found the
answer, and hit the step wall before emitting it.

The mean for that set was +17%. The distribution said something different:
amortization is real here and an identifiable bug is eating it.

There was no supported way for bundled code to reach a capability, while the
guidance was actively steering the agent into writing exactly that import. So
bundled code now executes with the session's builtins seeded into its globals,
and the docstring says so. Every call it makes is still routed through the
broker, policy-checked, budget-charged and audited, the same as a call from a
cell the model wrote by hand. A skill becoming code does not become a hole in
the containment model.

Re-running the arm after the fix:

- The bundled function worked on its **first call in all three reuse runs**.
  Zero `ImportError`s paid. No run wrote `import pyharness` in any cell.
- The audit chain shows the exact claim under test:
  `tools.invoke northwind_supplier_balance.get_northwind_balance`, then a
  nested `web.fetch` for the profile, then a nested `web.fetch` for the
  statement. Every hop separately gated and separately recorded.
- The best reuse run finished in 5 steps at $0.0128, **-53%** against its
  authoring run.

The mean moved less than the tail, and the reason is worth stating because it
is not a failure. Every reuse run also re-verified the skill's answer with
direct fetches, and one re-walked the whole chain. The task's own instruction
says to always re-read the source, and the scorer demands terminal-page
evidence. Re-verification is correct behavior. The frozen sequence means it
costs two fetches instead of five.

## The finding

A skill amortizes when the task's cost is **discovery**: working out which
pages, in which order, with what transformation. It does not amortize when the
cost is **retrieval**, because a procedure cannot lower the fetch floor, and
the reuse path has per-run overhead that must be paid out of whatever
discovery it removed.

On the retrieval task, that overhead exceeded the discovery saved. On the
discovery task, the saved discovery is roughly three fetches plus the reasoning
between them, which covers the overhead, as long as executing the skill does
not itself misfire.

The corollary is sharper than the finding: **save code, not prose.** A skill
whose markdown contains steps you could have written as a function has saved
the reader nothing, because the next model still has to read the steps, hold
them, and re-derive the code. The retrieval arm saved prose and lost 40%. The
discovery arm saved a function and its best run won 53%. That gap is not about
task shape alone. It is about whether the artifact is executable.

## Why this matters more for long-horizon work

These are small tasks. Fifteen seconds, two cents. The result generalizes in a
direction the dollar figures understate.

On a long-horizon task, the binding constraint is not cost per run, it is
context. Discovery is exactly the category of knowledge that does not fit: five
pages of navigation, a naming scheme inferred from a help article, a wrong turn
that had to be backed out of. Carrying that forward as transcript means
carrying the wrong turn too. Carrying it forward as a tested function means
carrying five steps as one call, and the reasoning that produced it does not
have to be re-read.

Run 5 is the shape to look at. Eleven to thirteen steps of discovery became
five steps of execution. Repeat that across a task with dozens of such
subgoals, over sessions long enough that the early transcript is gone, and the
saved library is the only thing that survives the context boundary.

Two caveats that get worse at that horizon, not better.

The saved skill froze the corpus server's ephemeral port into its URLs.
Within one set the server was one process, so it worked. Against a restarted
corpus it would fail on a stale origin. A frozen procedure encodes assumptions
that expire, which is why `record_skill_use` distinguishes `worked` from
`deviated`: a skill's trust has to be re-earned by a real run, not inherited
from having once been saved.

And bundling is not yet reliable. Across the two authoring runs after the
system-prompt change, one bundled code and one saved prose only. The behavior
that produces the win is not yet the behavior you can count on.

## What this does not say

Not a fact about skills in general. One corpus, one harness, two task shapes,
n=5 per arm. That supports a shape, not a statistic, and the discovery arm's
later-run costs span $0.0136 to $0.0427, which is the variance of one model
recovering from one error two different ways.

Not a fact about model choice: haiku-4.5 throughout. Not a clean cost
measurement either, since prompt caching is live and the trace records only
uncached input tokens, so per-run cost cannot be decomposed from the published
record. And run 1 is not a clean no-skill baseline, because it includes
authoring the skill.

The board publishes all of that, plus a disclosure of everything changed after
a paid run, including a scrapped $0.1949 set that is not published because a
fixture bug made the answer unreachable at any price. If you want to argue with
the conclusion, the per-run record is the place to start.
