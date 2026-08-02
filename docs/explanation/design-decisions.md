# Design decisions

The forks this project actually hit, the call I made at each, and the reasoning.
Written down because the interesting part of a system like this is rarely the
code — it is which of two defensible options got picked, and what that cost.

Every decision here is one I would defend in a review. Several of them are
decisions to *not* build something, and a few came out of being wrong first.

---

## The action space is Python, not a JSON tool schema

**The fork.** Give the model a catalogue of fine-grained tools and let it call
them, or give it one tool that runs code.

**The call.** One `run_python` call into a persistent kernel. There is no
`read_file` tool; there is `read(path)`, which is a Python function.

**Why.** Context economics, not elegance. With JSON tools, every intermediate
result travels back through the model's context to get anywhere — fetch a page,
the page comes back; filter it, the filtered version comes back. With a kernel,
state lives in variables and only what the agent `print()`s returns. A hundred
invoices can be parsed, joined and summed with three lines in context. Control
flow the model would otherwise have to unroll into a dozen tool calls (loops,
retries, conditionals) becomes one cell.

The cost is real and I took it deliberately: arbitrary code is a much harder
thing to contain than a fixed tool schema, and most of the rest of this file is
that bill coming due. See [The `run_python` action space](action-space.md).

## One dispatch chokepoint, decided at the start

**The fork.** Let each capability do its own I/O and add policy where it turns
out to be needed, or route every side effect through one function.

**The call.** Everything goes through `broker/dispatch.py`, which does
policy → audit → budget → execute in that order. A capability that wants to
touch the world asks the broker.

**Why.** This is the decision the whole project rests on, and it is only
available at the start. Retrofitting complete mediation onto a codebase that
scattered I/O across modules means finding every call site and trusting that you
found them all — and you cannot prove a negative about code you did not write
yet. Centralising first means the property is structural: a new capability that
skips the broker does not work at all, rather than working and being unaudited.

The idea is not novel. It is Saltzer and Schroeder's complete mediation, from
1975. What is scarce is an implementation without side doors, and that is a
consequence of when the decision was made, not of any cleverness in it.

## Agent code runs out of process, in an OS sandbox, by default

**The fork.** Trust the broker as the boundary, or assume the broker can be
walked around and put a real perimeter underneath it.

**The call.** Agent code executes in a child process under Seatbelt on macOS and
seccomp-bpf + Landlock on Linux. In-process execution still exists, for tests
only.

**Why.** A broker that is a Python object the agent's own code calls is advisory,
and in a runtime with arbitrary Python, `ctypes`, raw sockets, `os.system` and
`__import__` all route around advice. The honest fix is to remove the capability
rather than to ask nicely: the child has **no network syscall available**, so
there is no bypass to find. The only path outward is IPC to the parent, which
does the vetting.

This is the first thing a security-literate reader asks about a design like this,
and the answer needs to be a syscall filter, not a promise.
`make docker-verify` runs eight probes from inside real agent code to show it.

## Publish the gaps, with a reason each, and fail in both directions

**The fork.** Report what the defenses stop, or report what they stop *and* what
they do not.

**The call.** [`evals/SCOREBOARD.md`](../../evals/SCOREBOARD.md) publishes every
attack in four buckets: blocked, known gap, unexpected, error. Each known gap
carries a written rationale, and `make test` fails if any attack stops matching
what is published — including a gap that starts getting *blocked*.

**Why.** A gap with a stated reason reads as architecture. A gap without one
reads as unfinished work. Publishing them is also the only way the number means
anything: a suite that only shows wins is a marketing asset, and everybody knows
it, so it persuades nobody worth persuading.

The bidirectional gate is the part I would argue hardest for. A suite that
quietly under-claims — a gap silently closed while the docs still call it open —
stops being a source of truth just as surely as one that over-claims. Both
failures are the same bug: the artifact and the system drifted apart.

## Containment is measured as a control test, not by hoping a model takes the bait

**The fork.** Measure containment by pointing a model at hostile pages and
seeing whether the harness stops it, or by instructing the agent to do the
dangerous thing directly.

**The call.** Both, reported as separate numbers with separate denominators.

**Why — and I got this wrong first.** The first paid demo run cost $0.0359,
printed `2/2 hostile contained`, and was worthless. Haiku read both injected
payloads, named them in its own reply, and declined. Nothing was attempted, so
nothing was refused, and the board was green because the *model* behaved. That is
precisely the dependency this harness exists to remove.

The rebuild separates two questions that most injection evals average together:

- **Did the model comply with the injection?** A fact about the model. Reported
  with its own denominator, currently `0/2 acted on`, and it is not evidence
  about this harness at all.
- **Did the harness refuse the action?** Measured by a task carrying an ordinary
  operator instruction to release a credential — no injection, nothing to see
  through, and a call that is byte-identical at the broker to the exfiltration
  the payload was asking for.

If a system's containment number moves when you swap the model, it was never
measuring containment. This is the methodological point I would most want a
reader to take away, and it generalises well past this repo.

## Six tasks over four axes, not ten tasks and a pass@1

**The fork.** More tasks and a headline pass rate, or fewer tasks and several
honest denominators.

**The call.** Six tasks, four axes: competence, model compliance, containment,
authorized delivery. No single averaged number.

**Why.** Competence, compliance and containment are different questions, and
averaging them is exactly how the first run printed a green board having tested
nothing. Four honest denominators beat one dishonest one, and the eleventh task
adds a decimal place nobody will check. The scope cut was a downgrade in
apparent rigor and an upgrade in real rigor, which is usually how that trade
looks.

## A no-broker baseline, because a number needs a denominator

**The fork.** Report what the harness does, or report it beside what happens
without the harness.

**The call.** The same six tasks run through a naive tool loop with no broker,
same model, same pages, same attacker's listener.

**Why.** "The harness refused four releases" is unfalsifiable applause without
knowing what refusal-free looks like. With the control arm, the interesting
result is not the refusals: it is that **four of ten baseline runs hold the
credential in cleartext in the model's context**, and not because the model
misbehaved. With no vault there is nowhere else to put a secret the task
requires. The delta is structural rather than behavioural, which is a much
stronger claim than a pass rate, and it only exists because the control was run.

See [`evals/demo/COMPARISON.md`](../../evals/demo/COMPARISON.md).

## The corpus is raw saved HTML, replayed. No curation

**The fork.** Build a properly reproducible benchmark corpus — pinned assets,
snapshot tiers, drift measurement — or save the pages as-is and replay them.

**The call.** Save as-is. The hostile pages are hand-written, because the attack
is mine to author; everything else is a byte-for-byte capture.

**Why.** This is a portfolio artifact, not a published benchmark, and nobody is
going to re-run my suite to check my number. Importing benchmark-reproducibility
standards into it would have cost weeks and bought a property no reader will
exercise. Replay-from-disk survives on a different and smaller justification:
when the score moves, I want to know it was my code and not a site redesign.

Deciding how much rigor an artifact actually needs is itself a decision, and
over-applying it is a failure mode, not a virtue.

## No public interactive endpoint. Permanently

**The fork.** Deploy a hosted demo anyone can drive, or ship a container and
static session pages.

**The call.** No public agent endpoint, and this one is not provisional.

**Why.** Three reasons, any one sufficient. Every page the agent fetches is an
injection vector into a system whose entire pitch is containment. A public URL is
an unbounded spend faucet — `Budget` caps a session, not a stranger opening a
thousand of them. And if it does get popped, that becomes the most discoverable
fact about the project, permanently.

What ships instead is a container a stranger can run in five minutes with their
own key, and ten real sessions baked into static pages. That is a worse demo and
a much better decision, and I would rather be asked about this trade than about
an incident.

## Windows stays unconfined, and fails closed

**The fork.** Ship a partial Windows sandbox, or ship none and refuse to run
unconfined.

**The call.** macOS and Linux are confined. Windows has no OS sandbox and the
harness will not start there without an explicit `PYHARNESS_ALLOW_UNSANDBOXED`
opt-in.

**Why.** A confinement story that is 70% true on a platform is worse than none,
because the reader cannot tell which 30% they are standing in. Failing closed
makes the gap loud at the only moment it matters. The same predicate gates the
Linux floor: below Landlock ABI 3, the same refusal.

## Feature freeze: evidence over features

**The fork.** Keep building capabilities, or stop and prove the ones that exist.

**The call.** Froze the feature set and spent the remaining time on the
adversarial suite, the demo comparison, the threat model, the container and the
session pages.

**Why.** The repo was past "does it work" and had nothing anyone could check. One
more capability makes the surface wider and the claims thinner; the marginal
value of the twentieth capability is far below the marginal value of the first
real number. It also forces the honest question — *what does this actually let me
claim?* — which is the question that produced most of this page.

Held with two exceptions, both of which the evidence work itself surfaced: a
bug where bundled skill code ran outside the OS sandbox, and an egress guard that
vetted a hostname and then let the client resolve it independently. Fixing what
the measurement finds is the point of measuring.

## Rejected: repackaging this as a wrapper around other agents

**The fork.** An outside review recommended dropping the harness and shipping a
CLI that confines someone else's agent instead.

**The call.** Declined.

**Why.** Four reasons. "Same code, two front doors" is false: egress control here
is in-process Python policy, and enforcing it on a foreign process means a MITM
proxy, per-child network namespaces or nftables, none of which exists here.
It resets a project with days of work left by months. The headline claim fails on
the most obvious target, since Claude Code's sub-agents are in-process loops with
no PID to attribute egress to. And the competitive premise rested on several
citations I could not verify first-hand.

Recorded here because a rejected direction with reasoning is cheaper than
relitigating it, and because "the reviewer was sharp and still wrong on the
recommendation" is a normal outcome worth being able to explain.

---

## What this page is not

It is not a changelog and not a status report. Decisions that turned out to be
uninteresting, or that were forced by circumstance rather than chosen, are not
here. Neither is anything I am still deciding.

The things I would change with more time are in the threat model as
[published gaps](threat-model.md#the-published-gaps), which is the right place
for them: a known weakness belongs next to the security claim it qualifies, not
in a document about how good the reasoning was.
