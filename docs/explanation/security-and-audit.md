# Security & audit

The trust boundary is simple to state: **the agent writes untrusted code; the
harness decides what that code is allowed to do, keeps secrets out of its reach,
and records everything.** Four mechanisms, all sitting at or behind
[the broker](broker.md).

## Policy — what may run

`pyharness/security/policy.py` judges each action (`"<capability>.<operation>"`)
and returns one of three decisions:

- **ALLOW** — proceed (the default; pyharness is allow-by-default).
- **DENY** — refuse and record it.
- **APPROVE** — ask a human `approver` first; refusal is recorded as denied.

Rules match by prefix, so `"files"` gates every file operation and
`"files.write"` gates just writes. The default policy requires approval for
`skills.save_skill`, `skills.edit_skill`, and `packages.install` — all write
content that would load and run in later sessions, so a human signs off at
author time (this holds for the [reflection pass](../how-to/observability.md#post-session-reflection)'s
proposals too — reflection routes its skill writes through the same broker gate). It also gates
**state-changing HTTP** (`http.request` with POST/PUT/PATCH/DELETE) and
**state-changing browser actions** (`click` / `fill` / `fill_secret` /
`select_option` / `press` / `upload`), since those act outward on the user's
behalf; reads, navigation, `snapshot`, `scroll`, and `wait_for` stay free.

Most rules match on the action name alone, but a rule can also judge a call from
its arguments: `Policy(approve_if=[predicate])` runs each predicate over
`(action, args, kwargs)` and forces approval if any returns true. That is how one
action (`http.request`) is gated on a *value* (the HTTP method) rather than
needing a separate action name per method.

### What the human is shown — preview and taxonomy

An approver is not handed raw arguments to squint at. The broker builds an
`ApprovalRequest` (`broker/dispatch.py`) carrying a **category** and a
human-readable **summary**, and the audit log records the category alongside the
decision:

- **`category`** (`security/policy.py:ActionCategory`) grades severity —
  `LOCAL` (stays in the workspace, e.g. `save_skill`), `OUTWARD` (sends off-box
  or acts on a remote page, e.g. a POST or a browser click), or `IRREVERSIBLE`
  (a remote effect the harness knows can't be undone, e.g. an HTTP `DELETE`). The
  harness assigns this; an agent cannot grade its own actions' risk, and page
  text can never talk it down a rung.
- **`summary`** is a short, secret-safe line describing the effect — `POST
  https://…/apply (body: name, resume_id)` names the target and the body *fields*
  but never their values; a browser click is shown with the page it lands on
  (redacted through that session's secret sink, so a query-string secret in the
  url never surfaces in the confirmation).

A capability that owns gated ops supplies `preview(op, args, kwargs) ->
(category, summary)`, so the arg-shape knowledge stays with the capability;
anything else falls back to a conservative `OUTWARD` classification. The CLI
approver prints `⚠ approval required [category]: action` then the summary, and
asks `allow? [y/N]` (or `[y/a/N]` when a grant is on offer — see below).

> The `ApprovalRequest` is built from the **structured** call, never an
> agent-supplied display string — so what a human sees is exactly what executes.

### Scoped grants — approve a domain, not every click

A 20-field job application is ~21 separate approvals if every `click`/`fill`/POST
prompts on its own. A **grant** lets a human approve a *class of action on a host*
once, so matching calls flow without re-prompting — while the risky ones still
always ask.

At a grantable prompt the CLI offers a third choice:

```
⚠ approval required [outward]: browser.click
  click [ref=e12] 'button "Submit application"' on https://boards.greenhouse.com/acme
  [y] this once  [a] all state-changing browser actions on boards.greenhouse.com this session  [N] no
  allow? [y/a/N]
```

Answering `a` mints a grant for `(action-class, host)` — here `("browser",
"boards.greenhouse.com")`. The `GrantLedger` lives on the `Broker` and is checked
on the APPROVE path *before* the human is asked; a live matching grant
auto-approves the call. The mechanics and the invariants that keep it safe:

- **The scope is harness-derived**, never agent- or page-supplied. A capability's
  optional `scope(op, args, kwargs) -> GrantScope | None` hook (mirroring
  `preview()`) yields the key from the structured call: the host from the request
  `url` or from Playwright's own `page.url`. The `[a]` label is rendered from a
  fixed class-name map plus that host — no page text reaches it.
- **IRREVERSIBLE is never covered and never mints.** A `DELETE` re-prompts every
  time, even on a host you granted POSTs to; the ledger is consulted only for
  non-IRREVERSIBLE calls. `fill_secret` (credential release) and the secret-gated
  `look` are likewise excluded — their `scope()` returns `None`, so they always
  ask.
- **Exact match, no wildcards.** A grant on `boards.greenhouse.com` does not cover
  `api.greenhouse.com`; there is no "all hosts" or "all actions" scope. If the
  page navigates to another host, subsequent actions match the new host and
  re-prompt.
- **Grants never widen policy.** They short-circuit only the *prompt*, never the
  decision — a `DENY` still denies, and `approve_if` predicates still run.
- **Audited in the hash chain.** Issuance rides the approving call's entry
  (`grant: {id, action_class, target, expires_at}`); each covered call records
  `grant_id`; revocation records `{event: "grant_revoked", grant_id}`. The agent
  sees all of this via `history()`.
- **Session-lifetime, in-memory.** The ledger dies with the `Session`; nothing
  persists across sessions, so "for this session" is literally true. Standing
  policy has its own home — construct a `Session(policy=…)` without the gate.
  (`GrantLedger.add` takes an optional `ttl_s`; the CLI mints session-lifetime
  grants only. Plan-scoped grants are deferred to the recursive-`spawn` work.)

## Vault — secrets the agent can name but never read

`pyharness/security/vault.py` holds one hard rule: **no capability exposed to
agent code ever returns a secret's cleartext.** The agent sees only names (via
the `secrets()` builtin); the value is resolved in the parent and injected at the
point of use — on the discovered web/http/browser tools, not in agent-visible
text (e.g. `web.fetch(url, auth="github")`, or `http.request(..., auth=...)`
and `http.request(..., secret_fields={"password": "name"})` for a stateful HTTP
session, or `browser.fill_secret` typing into a browser field — every action
surface is another injection sink under the same rule, not an exception to it).
`Vault.get()` is deliberately *not* in the kernel namespace.

Each injection surface routes through one small primitive,
`security/sink.py:SecretSink`, scoped to a single injection context (one browser
session, one HTTP request). It is the only place a name becomes cleartext, and it
records every value it resolves so the capability can mask it (`***`) back out of
anything the agent then reads — a browser `read_text` or `snapshot` tree, or an
HTTP response `url` (a `"query"`-style secret can survive into the final url),
`text`, or `headers`. A resolved secret never round-trips through agent-visible
output.

Masking works on text; pixels it cannot reach. A secret typed into a page is
visible in a screenshot, so `browser.look` (which puts a screenshot in the
model's context) is gated by the default policy once a session has injected a
secret — an argument-dependent `approve_if` predicate, the same mechanism that
gates state-changing HTTP by method. `screenshot` only writes to disk, so a
secret on-screen still lands in that file; keep credential entry on the `http`
path where the value never renders.

Resolution order, first hit wins: in-memory dict → environment
(`PYHARNESS_SECRET_<NAME>`) → encrypted file. The file is sealed with a
passphrase-derived key (scrypt) and Fernet (authenticated AES); a wrong
passphrase fails to decrypt rather than returning garbage. See
[Use the secrets vault](../how-to/use-the-vault.md).

## The out-of-process sandbox

When the agent runs in a child process (`out_of_process=True`), two OS-level
layers confine it (`pyharness/broker/remote/sandbox.py`), on top of the process
boundary itself:

- **Env scrubbing** — secret-bearing variables (`PYHARNESS_SECRET_*`, the vault
  passphrase, and the provider API keys the parent uses to call the LLM — e.g.
  `ANTHROPIC_API_KEY`) are deleted from the child's environment before any agent
  code runs, and from any subprocess the child spawns. The child has no LLM client
  of its own (completions route through the broker), so it needs none of them, and
  even `printenv` can't read a key the parent legitimately holds.
- **macOS Seatbelt** (`sandbox-exec`) — enforces the perimeter, not a blanket
  lockdown. The guiding rule is **the workspace is the sandbox; the broker guards
  everything that leaves it.** Agent code reads and writes freely *inside* its
  session workspace (so libraries that persist files — `savefig`, `to_csv` — just
  work, and the child's working directory *is* the workspace), while three channels
  that would bypass the broker are denied: outbound **network**, filesystem
  **writes outside the workspace**, and **reads of the user's personal files** (a
  read jail hides `$HOME`, re-allowing only the interpreter and pyharness's own
  package source the child needs to import — the package's `sys.path` directory is
  listable so the import resolves, but its other files, a project `.env` or a prior
  session's data among them, stay unreadable). Anything the child execs inherits
  the profile.
- **POSIX rlimits** — no core dumps; on Linux, a process cap to blunt fork bombs
  (skipped on macOS, where the limit is per-user and would break ordinary
  `subprocess`/`fork`).

All are best-effort and degrade silently where a platform can't honor them. On
non-macOS, only the resource limits apply (seccomp/namespace confinement isn't
built yet). The child needs no network and no writes outside its workspace:
every side effect that leaves the box goes back through the broker in the parent,
while scratch files stay in the workspace where both the agent and the human can
see them.

> Workspace-internal writes are deliberately *not* individually audited — the
> audit chain records effects that cross the perimeter, not every `savefig`. The
> workspace is inspectable on disk; the broker is where outward actions are gated
> and logged.

## Audit — a tamper-evident record

`pyharness/audit.py` appends every capability call to `audit.jsonl` as a hash
chain: each entry stores `hash = sha256(prev_hash + entry)` and a `prev` pointer.
Any later edit, deletion, or reordering breaks the chain, so the log is
verifiable — which matters once it's shipped off-box.

Secrets are never arguments to capabilities (they're referenced by name and
injected in the parent), so logged arguments are safe to persist.

The same record is the agent's own reflection surface. `audit.jsonl` sits at the
session root, outside the workspace the file builtins are confined to, so agent
code can't read it directly; the `history()` builtin exposes it read-only. That
closes the *observe* half of the agent's do → observe → revise loop — it can
confirm an effect landed, or see why an action was refused, and revise a saved
skill from what actually happened. The internal chain fields are dropped from
what `history()` returns; arguments are already the log-safe summary.

Verify a session's chain:

```bash
make verify-audit DIR=.sessions/<name>
# → "✓ intact"  or  "✗ broken at entry N"
```

or in code: `from pyharness.audit import verify_chain`.

`audit.jsonl` is the always-on, local source of truth; the
[observability](../how-to/observability.md) layer is a queryable view on top of
the same events, not a replacement.
