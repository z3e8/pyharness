# Threat model

[Security & audit](security-and-audit.md) describes the mechanisms. This page
states the **perimeter** they add up to: who the adversary is, what the box
actually confines on each platform, and every gap that is known and published —
each with the decision that produced it.

The gaps are here because a perimeter nobody has drawn the edge of is not a
perimeter. Every one of them is reproduced as a scored attack in
[`evals/SCOREBOARD.md`](../../evals/SCOREBOARD.md), which `make test` fails on
in **both** directions: a gap that quietly gets closed breaks the suite exactly
like a defense that quietly breaks.

## The adversary

Three, in the order they drive the design.

1. **The agent's own code.** The model writes Python; the harness runs it. The
   agent is not assumed malicious — it is assumed *unverified*, which for design
   purposes is the same thing. Everything reachable from the kernel namespace is
   a broker proxy, so there is no callable that skips policy → audit → budget.
2. **Content the agent reads.** A web page, an MCP tool result, an email body —
   all attacker-controlled text. The design answer is that reads return
   untrusted input and nothing in that input authors a decision: approval
   prompts and grant scopes are built by the harness from the *structured* call,
   never from a display string the agent or a page supplied. Injected text can
   ask for anything; it has nowhere to land.
3. **Someone holding the log afterwards.** The audit chain is tamper-evident, so
   an account of a session can be checked by a party who did not watch it
   happen.

### Explicitly outside the model

- **The parent process.** The broker runs *unsandboxed* — it has to, since it is
  what performs the side effects the child is denied. A bug in the harness
  itself, or a malicious dependency imported parent-side, is not something the
  harness defends against.
- **Anyone with write access to the session directory.** They can rewrite
  `audit.jsonl` and its anchor together. Agent code is *not* such an attacker on
  a sandboxed platform: the log and the anchor live one level above the
  workspace the write jail confines it to.
- **The model provider**, and anyone who can substitute the completions.
- **A resolver you do not control** — see the DNS-rebinding residual below.
- **A human who approves everything.** Prompt fatigue is treated as a *design
  constraint* rather than an attacker: published gaps 1 and 4 below exist
  precisely because a prompt asked too often stops being a decision. That
  tradeoff is stated at each one rather than hidden.

## The perimeter, per platform

Confinement is built for two platforms, and claimed for exactly those two.

| Agent code in the child can… | macOS (Seatbelt) | Linux, Landlock ABI 3+ | Windows · Linux below the floor |
|---|---|---|---|
| open an outbound socket | no | no (seccomp on `AF_INET`/`AF_INET6`/`AF_PACKET`) | **yes** |
| write outside its workspace | no | no (Landlock) | **yes** |
| read `$HOME` | no | no (Landlock) | **yes** |
| read the parent's `/proc/<pid>/environ` — where your API key and vault passphrase live | n/a (no `/proc`) | no (Landlock hooks `ptrace_access_check`, which also denies `PTRACE_ATTACH`) | **yes**, on Linux below the floor |
| exhaust resources | core dumps off | core dumps off, process cap | Linux: same rlimits · Windows: **none** |
| **start at all, by default** | yes | yes | **no — refuses** |

The floor on Linux is **Landlock ABI 3 (kernel 6.2)** on x86-64 or aarch64,
because ABI 3 is the first with `LANDLOCK_ACCESS_FS_TRUNCATE`; below it,
`truncate(2)` on an already-open descriptor escapes the write jail, so an older
kernel reports *no sandbox* rather than a jail with a hole. Both backends are
applied by the process to itself — no helper binary, no user namespaces, no root
— and both are irrevocable and inherited across `exec`, so they hold inside an
ordinary container and cover any subprocess the agent spawns.

**Windows is unconfined by design, and fails closed.** No backend is written for
it, and the honest response to that is to refuse to run rather than to claim a
perimeter that does not exist. `check_unsandboxed_platform()` runs when the
session constructs its out-of-process kernel — before any agent code, before any
LLM spend — and raises. Running anyway takes the explicit
`PYHARNESS_ALLOW_UNSANDBOXED=true` opt-in and prints a one-time stderr warning
that agent code is unconfined. Where a sandbox exists the gate is a no-op and the
variable is ignored, so the opt-in cannot weaken a platform that has a perimeter.
The same gate covers a Linux kernel below the ABI floor. The CI workflow runs the
full suite on Ubuntu (3.11/3.12/3.13) and macOS **without** the opt-in set, which
is what makes "confinement is in force" an observation rather than a claim: a
runner below the floor turns the run red instead of silently running unconfined.
That workflow is dispatched by hand rather than on every push, so a green run is
evidence dated to when it ran, not a standing guarantee about `main`.

### What is inside the box and what is beside it

The *child* is sandboxed. Everything else that execs a program parent-side is
classified in writing, four ways — the exemption table is what the enumeration
tests hold to:

- **`shell.bash` and `packages.install` are wrapped** in the same OS profile as
  the kernel (`packages` differs in one way — pip is allowed to reach the index,
  with writes confined to the session venv plus scratch). Both are also
  approval-gated per call and never grantable.
- **Playwright's Chromium** is launched by the playwright package, outside the
  harness sandbox profile. The perimeter claimed for the browser has always been
  navigation scoping plus per-action approval — never a network policy. Two of
  the gaps below are consequences of that.
- **A local (stdio) MCP server** is exec'd with the minimal allowlist
  environment but no sandbox wrap. Mounting one requires human approval and is
  never grantable: an approved local MCP server runs with the operator's OS
  reach.
- **Fixed harness-authored argv** — venv creation, the desktop-notify helper —
  is unwrapped because no agent-controlled argument reaches it.

**A learned skill's bundled code is inside the box, not beside it.** It is the
one class of *agent-authored* code that is not a cell, so it is confined like
one: `use_tool` ships the skill's source across the IPC boundary and the child
executes it, under the same sandbox profile, with the same broker proxies in
scope. It ran parent-side until 2026-08-01, which made it the sharpest hole in
this table — approval-gated, but unconfined and unaudited once approved. Two
things follow from the fix. The move costs no mediation, because a skill's only
reach outside the process was ever the builtins in its globals, and in the child
those are the same proxies a cell calls. And a save's approval prompt now shows
the bundled source, since approving a skill is approving code that runs later.

That leaves the surface where a *time-delayed* injection lives: content planted
on one run and fired on a later one, after the approval that admitted it can no
longer be revisited. The `skills` rows on the scoreboard measure both directions
— what a stored procedure can still reach when it fires (the later session's
boundaries govern, not the authoring session's) and what the sign-off that
admitted it actually showed.

Writing them surfaced a gap in the second half and closed it. The prompt showed
a skill's bundled code and never its markdown, and `edit_skill`'s prompt was a
count of how many deltas were being applied. For a CodeAct agent that is the
wrong half to disclose: the instructions are what `describe_tool` puts into a
later run's context and what the model then follows, so the prose is executable
in every sense that matters here. Both are now rendered, capped and elided in the
middle so an instruction appended to a long procedure still appears and the human
is told when text was dropped. `skill-text-approved-unseen` scored it open and
now pins it shut. Gap 8 below is the one that remains.

## Dispatch is centralized; containment is not

This is the load-bearing structural fact about the codebase, and it is the
reason the enumeration tests exist.

`broker/dispatch.py` is a genuine choke point. Containment is not one mechanism
behind it — it is several (host scoping, sandbox wrapping, secret-sink
mirroring), and each is implemented **per capability**. So every capability added
is an opportunity to silently opt out of one.

The 2026-07 security recon found four unrelated-looking bugs that were that one
bug wearing four hats:

- `allowed_hosts` was threaded through 3 of the capabilities and not the rest;
- `packages.install` skipped the sandbox wrap `shell.bash` already had;
- the MCP-over-HTTP transport dropped the scope argument;
- browser subresource loads skip the scope check.

Three were fixed. The fourth is a stated boundary (below). But the durable fix is
not those three patches — the next capability would have reintroduced the shape.
It is `tests/test_capability_policies.py`, which for each cross-cutting policy
enumerates every capability **from the live broker registry** — never from a
hand-written list — and forces each into one of two states:

- it *enforces* the policy, with the wiring asserted on the live instance (e.g.
  `cap.allowed_hosts == session.allowed_hosts`, `cap._sink_mirror is
  session.secret_sink`), or
- it is a *named exemption carrying a written rationale*, which the test
  length-checks so a stub cannot stand in for a decision.

Five policies are covered: host scoping, parent-side sandbox wrapping, dispatch
mediation, approval classification, and secret-sink wiring. Registering a
seventeenth capability without classifying it fails the three that partition over
the whole registry (host scope, agent-facing surface, approval), and the other
two are detector-driven rather than declarative — a newcomer that execs a program
or takes the `Vault` fails those as well, and one that does neither has nothing
to classify. The enforcement assertions are not vacuous either: un-threading the
session scope from a single capability fails the host-scope test even though the
attribute is still there.

That does not make gaps impossible. It makes them **impossible to leave
undecided**, which is the property that survives the next contributor. The
exemption tables in that file are the authoritative list of stated design
boundaries, and every rationale below is asserted there or in the scoreboard, so
prose and behavior cannot drift apart.

## The published gaps

**37 of 49 adversarial attacks blocked. 12 known gaps, 0 unexpected successes, 0
errors.** The per-attack rationales are in
[`evals/SCOREBOARD.md`](../../evals/SCOREBOARD.md); what follows groups them by
the *decision* that produced them, because there are fewer decisions than gaps.

### 1. A grant's unit of trust is coarser than the prompt's

`host-grant-covers-any-path` · `spawn-grant-covers-wider-child` ·
`mcp-grant-covers-another-tool`

Grants are keyed on `(action class, host)`, on `("spawn", "session")`, and on the
MCP server rather than the tool. In each case the human is shown one concrete
thing and grants a slightly wider class of it. The alternative — a grant keyed on
the exact request, the exact child, the exact tool — re-prompts on every URL of a
normal multi-step task and on every tool of a twenty-tool server, and the
reliable outcome of prompt fatigue is that humans approve everything. Precision
on paper, lost in practice.

What bounds it: a grant never widens policy, only short-circuits the prompt;
IRREVERSIBLE actions are never covered and never mint; grants are exact-match
with no wildcards and die with the session; the reach is still bounded by the
session's host scope; and issuance, every covered call, and revocation all land
in the audit chain.

### 2. The browser is outside the harness's network perimeter

`browser-subresource-off-scope` · `browser-websocket-unvetted`

Host scope applies to **main-frame navigations** — where the agent goes — not to
what a page loads. Scoping subresources means blocking every CDN, font and
analytics host a real site depends on, which breaks the page the agent was sent
to read. And the browser's enforcement point is HTTP request interception, which
does not see WebSocket traffic at all: there is no check to fail, which is worse
than a check that is too lenient.

The WebSocket half is the widest gap in the suite and the least defensible on
design grounds.
What makes it a boundary rather than a bug is where the browser sits — a full
Chromium running beside the harness sandbox, for which the claim has always been
navigation scoping plus per-action approval. Subresource loads still pass the
SSRF guard, so internal and link-local targets stay refused, and every mutating
browser action still needs a human. **The operational consequence, stated
plainly: a session that must not leak should not be given the browser.** The
`http`/`web` lanes are fully scoped and are the contained way to read the web.

The WebSocket hole is pinned by a test that fails if it is ever closed, so this
section cannot go stale silently.

### 3. Masking is a backstop, not the boundary

`secret-re-encoded-in-response` · `secret-via-exception-attribute`

Redaction of agent-visible text is a literal substring replace over a secret and
its URL-encoded spellings. It catches the incidental verbatim echo — the common
case — and deliberately not a value a server re-encoded (base64, hex, HTML
entities, split across chunks). Relatedly, whether a failure is carrying a
credential is decided from what the failure *says* (its message and repr), not by
rewriting arbitrary objects' attributes, which would mean either destroying the
error type agent code catches on or walking unbounded object graphs.

Widening either would trade a few more catches for `***` appearing over innocent
text and for errors the agent can no longer handle. The boundary that actually
holds is upstream: a resolved secret only ever travels to a host the vault and
the human sanctioned, and never enters agent-visible output by design. The server
in a position to re-encode a credential is one that was already given it.

### 4. Reads are free, so arbitrary data can leave via a GET

`unapproved-data-exfil`

The approval gate fires on the *release of a credential the harness holds* and on
state-changing methods. A GET carrying a string the agent already had is neither.
Gating it means classifying arbitrary outbound content as sensitive, which cannot
be done reliably and would put a prompt in front of ordinary work.

The stated boundary for arbitrary data is therefore the **host scope**, not
approval — the companion attack `scoped-data-exfil` shows the same exfiltration
refused outright inside a confined session — plus the audit chain, which records
every request whether or not anyone was asked. This is the single most important
sentence for anyone deciding how to run a task: *if the data matters, scope the
session.* The next gap is the fine print on that sentence.

### 5. A scope answers "which host", never "what is being sent"

`scope-abuse-in-scope-channel`

Gap 4 points at the host scope as the boundary for arbitrary data, and every
scope attack on the board tries to get **out** of a scope and is refused —
lookalike domains, IP literals, redirects, a rebound hostname. This gap is the
one that stays **in**.

`allowed_hosts` and the egress check vet a *destination*. Neither looks at the
payload. So any permitted host that accepts free text the attacker can read back
later — a search query, an issue comment, a support ticket, a filename — is a
channel that passes every check by construction. The injected instruction does
not need to escape the scope; it needs to encode the secret into a field of a
site the session was always allowed to talk to.

This is deliberate rather than missed. Deciding whether an outbound field carries
something sensitive is content filtering: it cannot be done reliably, and it is a
posture this threat model does not take (see *Explicitly outside the model*). The
honest boundary is that **the perimeter constrains where data can go, not what
can be encoded into an allowed destination.**

What bounds it in practice: the scope is chosen by a human who is shown it, so
narrowing it narrows the channel to hosts that were deliberately trusted; and
every request lands in the audit chain, so this is a *permitted* channel, not a
silent one. The operational rule that follows: a session handling data that must
not leak should be scoped to hosts with no readable free-form surface, or given
no network capability at all.

The reason this is published rather than left implicit: a reader who scans the
egress rows, sees four scope escapes refused, and concludes the surface is
covered would be wrong in a way the board itself invited.

### 6. The audit anchor is not a keyed MAC

`audit-remove-the-anchor`

A hash chain proves internal consistency. Detecting deletion of its *end* needs a
trust root the attacker cannot reach, which means either a key store this harness
does not have or shipping the head off-box; inventing a local one would only look
like a guarantee. A missing anchor is therefore treated as "no trust root" rather
than as evidence, because a log that never had one is indistinguishable from one
whose anchor was removed.

The claim is exactly what it says: the chain is tamper-**evident** against anyone
without write access to the session directory. Agent code is not such an attacker
by construction — the OS sandbox confines its writes to the workspace, and both
`audit.jsonl` and its anchor live one level above it — so reaching this gap means
already being outside the box.

### 7. Host scope does not narrow going down — the one open gap

`child-scope-wider-than-parent`

Every other gap here is a defended trade-off. This one is stated as **open, not
defended**: a child's host scope is normalized and wired in but never intersected
with the parent's, so a scoped parent can ask for a child with a wider one. The
natural reading of the feature is that a scope follows delegation down, and here
it does not.

It is a disclosure failure rather than a silent escape: reaching it needs a human
to approve a spawn whose prompt lists the child's hosts explicitly, the child
still cannot delegate further (depth is one), and it cannot exceed the parent's
budget slice. The human is shown the widening and has to accept it.

### 8. A skill's "this worked before" marker is self-reported

`skill-marks-itself-verified`

`record_skill_use` is the one skills op that is not gated, deliberately: it
writes journal metadata rather than the procedure, and a prompt after every run
of every skill is the fastest way to teach a human to click through prompts.

The cost is that the marker a later run is shown — *verified: yes, the steps
below have run as written* — means "the agent said so". Nothing watches a skill
run and decides whether it worked. An agent steered by injected content can
assert it about a procedure that never executed, as can an honest agent with a
generous view of its own output, which is the likelier case. The `check` field
exists to give that judgement something to rest on, but running it is a
convention rather than an enforcement.

What bounds it: the marker is advice, not permission. A verified skill gets no
capability an unverified one lacks and every call it makes is gated identically,
so this changes what the agent is *told*, never what it may do. And the claim
itself goes through the broker — ungated is not unaudited — so "verified" can
always be traced back to the run that asserted it. The free-text `note` a use
carries reaches a later run's context the same way and is bounded the same way.

## What the suite deliberately does not contain

Several candidate attacks were considered and not written, because each would
report the wrong thing:

- **`shell` / `packages` sitting outside the host scope** and **`packages.install`
  build hooks** are contained by the OS sandbox. On macOS and Linux the exploit
  does not succeed, so scoring them as gaps would be false; on an unsandboxed
  platform they would succeed. A platform-dependent verdict is worse than an
  absent one.
- **Agent code writing to `audit.jsonl`** is the same problem inverted — blocked
  on the supported platforms, reddening CI anywhere else for a reason that says
  nothing about the harness.
- **Agent code planting a skill with raw filesystem calls** is the same shape
  again: the skills root lives under `~/.pyharness`, which the child's read jail
  covers, so the exploit fails on macOS and Linux and would succeed with no
  sandbox. What *is* scored is the brokered file lane —
  `skill-planted-through-the-file-lane` — which is jailed in plain Python
  everywhere, so a procedure cannot reach the skills root without the sign-off.

All of them live where they belong: as written exemptions in the policy
enumeration tests, whose rationale names the OS sandbox as the containment, with
`tests/test_shell_sandbox.py` and `tests/test_linux_sandbox.py` asserting the
floor directly.

## Residual risks that are not scored anywhere

- **Local execution is contained but not audited.** A cell can call
  `subprocess.run` (or `os.execv`, or libc via `ctypes`) and no audit record is
  written, while `shell.bash` — the same act, routed through the broker — is
  approval-gated and chained. The asymmetry is deliberate on both halves.
  *Contained:* whatever it spawns inherits the child's sandbox and reduced
  environment, so it reaches no network, writes nothing outside the workspace,
  and reads nothing under `$HOME` (asserted by
  `test_sandbox_is_inherited_by_a_subprocess_agent_code_spawns` and
  `test_no_escape_by_exec`). *Not audited:* the chain records what crosses the
  perimeter, not what happens inside the box — and no other option exists, since
  any interception placed in the child is advisory against code that owns that
  process. `bash` is gated for a different reason: it runs parent-side, where
  the jail is applied by a wrap rather than inherited. The practical cost is
  that an agent hitting approval fatigue on `bash` has an unaudited alternative,
  so per-command usage numbers are a floor, not a census.
- **Egress on the two paths that cannot be pinned.** The `http` capability and
  remote MCP now connect to the address the guard vetted, so neither a racing
  resolver (DNS rebinding) nor a hostname the guard and the client read
  differently (IDNA) reaches an unvetted host — both are scored,
  `ssrf-dns-rebinding` and `ssrf-idna-confusion`. Two paths keep only the
  name-based check: the **browser**, because Playwright owns Chromium's sockets
  and nothing in-process can pin them (every request is re-vetted by the route
  interceptor instead), and any session **behind an HTTP(S) proxy**, where the
  socket goes to the proxy rather than to the vetted address. Both keep the
  original resolve-then-connect race.
- **Anything reached by compromising the parent**, per the adversary model above.
- **`sessionStorage`-based logins** are not captured by a saved site profile, and
  profile auto-refresh persists every cookie the context accrued — so keep a
  profile session on its own site.

## Checking any of this yourself

```bash
make evals                            # re-run the 49 attacks, rewrite the scoreboard
make test                             # the suite plus the policy enumeration tests
uv run pytest tests/test_capability_policies.py -q   # the exemption tables, asserted
make verify-audit DIR=.sessions/<name>              # a session's chain: ✓ intact / ✗ broken at N
```

The mechanisms behind every claim here are in
[Security & audit](security-and-audit.md); the routing they sit on is in
[The broker](broker.md).
