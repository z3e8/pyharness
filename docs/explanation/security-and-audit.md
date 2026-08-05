# Security & audit

The trust boundary is simple to state: **the agent writes untrusted code; the
harness decides what that code is allowed to do, keeps secrets out of its reach,
and records everything.** Four mechanisms, all sitting at or behind
[the broker](broker.md).

> This page is the *mechanisms*. For the perimeter they add up to — the
> adversary model, what is confined on each platform, and all ten published
> gaps grouped by the decision behind them — see the
> [threat model](threat-model.md).

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
proposals too — reflection routes its skill writes through the same broker gate).
A `save_skill` prompt shows the **bundled source** it is approving, not just the
skill's name: that code is what a later run executes.
`shell.bash` is approval-gated too, and the reason is narrower than it looks: it
executes **parent-side**, so its jail is applied by a per-platform wrap
(`sandboxed_shell_argv`) that has to be right, and that falls back to no jail at
all on a platform without an OS sandbox. It is *not* gated because running a
program is more dangerous than running a cell — a cell's own `subprocess.run` is
an arbitrary program too, and it inherits the child's jail rather than needing
one applied. See [local execution is contained but not
audited](threat-model.md#residual-risks-that-are-not-scored-anywhere). It also gates
**state-changing HTTP** (`http.request` with POST/PUT/PATCH/DELETE) and
**state-changing browser actions** (`click` / `fill` / `fill_secret` /
`fill_totp` / `select_option` / `press` / `upload`), since those act outward on the user's
behalf; reads, navigation, `snapshot`, `scroll`, and `wait_for` stay free.

Two argument-dependent gates guard the seams where a "free" read could still leak
a secret:

- **Any request that attaches a vault secret** (`http.request` or `web.fetch` with
  `auth`/`secret_fields`) requires approval *regardless of HTTP method* — sending a
  credential off-box is a credential-release action, so a `GET` (otherwise free)
  can't quietly ship a named secret to an attacker host. The approval summary names
  the secret (`auth=github via header`) so the human can catch a token headed for
  the wrong destination, and it is grantable per host so repeated authenticated
  reads to a vetted host don't re-prompt. A secret-carrying request also stops
  following redirects, so a cross-origin `Location` can't resend a custom-header or
  body credential to a host the prompt never showed.
- **`browser.screenshot`** is gated exactly like `browser.look` once a secret has
  been typed into the session — the PNG carries the credential's pixels (which text
  redaction can't mask) and the agent can read the file straight back out of the
  workspace.

Reads being free rests on a second rule: **what a read returns is untrusted
input.** A web page, an MCP result, and an email body (`inbox.read` — anyone
can send mail to the account) are attacker-controlled text; nothing in them can
widen a grant, alter policy, or author an approval prompt, because grants and
prompts are harness-built from the structured call (below) and mutations still
gate on a human regardless of what the text asks for. The inbox narrows its
half further by construction: it has no mutating op at all — no send, delete,
flag, or move exists to be talked into.

Most rules match on the action name alone, but a rule can also judge a call from
its arguments: `Policy(approve_if=[predicate])` runs each predicate over
`(action, args, kwargs)` and forces approval if any returns true. That is how one
action (`http.request`) is gated on a *value* (the HTTP method) rather than
needing a separate action name per method.

**MCP tool calls** gate the same way. Every call on a loaded tool module routes
through one action, `tools.invoke`, and a default predicate
(`broker/capabilities/tools.py:unvetted_mcp_call`) forces approval when the
target is an MCP server tool — unless the server's descriptor declares
`readOnlyHint`. An explicit `destructiveHint` classes the call IRREVERSIBLE
(always re-asks, never grantable); everything else, including un-annotated
tools, is OUTWARD and grantable per server (below). Two caveats are deliberate:

- Annotations are *server-supplied* hints. Trusting them is acceptable because
  installing the server is itself human-gated (`tools.add_mcp_server` requires
  approval; the config file is human-edited) and the per-call prompt guards
  against agent mistakes, not server malice.
- The MCP spec reads an *absent* `destructiveHint` as destructive. Taking that
  literally would class most tools IRREVERSIBLE and remove the
  one-grant-per-server flow, so pyharness prompts (grantable) instead of
  always-re-asking for the un-annotated case.

Deciding policy never connects a server: an MCP target that is not yet resolved
fails closed (prompts). Per-server rules are predicates over `tools.invoke`'s
arguments (`args[0]` is the server, `args[1]` the function), the same pattern as
the HTTP-method gate. Mounting a server at runtime (`tools.add_mcp_server`) is
itself in the default `require_approval` set — it installs code, like
`packages.install` — and refuses a name already in the registry, so a server
can't shadow `http`/`web` and launder its approval summaries.

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

A capability may also supply `validate(op, args, kwargs)`, which runs *before*
the prompt and raises if the call cannot run at all. The browser uses it for
`ref=`: a ref from a stale snapshot is refused when the op executes anyway, so
without this hook the human is asked to approve typing a credential into a page
for a call guaranteed to raise. Approvals only mean something if each one is
load-bearing, so a doomed call must never spend one. It runs after the policy
deny check — a denied action stays denied, and validation cannot be used to probe
policy with crafted arguments.

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
  `url` or from Playwright's own `page.url`, or — for MCP calls — the server's
  registry name (`("mcp", "github")`), so one grant covers that server's
  non-destructive tools for the session. A sub-agent has no host, so `spawn`
  yields a fixed session-wide key `("spawn", "session")` and one `[a]` covers a
  whole report's fan-out (`[a] all sub-agent spawns this session`) instead of
  re-prompting per child. The `[a]` label is rendered from a fixed class-name
  map plus that target — no page text reaches it.
- **IRREVERSIBLE is never covered and never mints.** A `DELETE` re-prompts every
  time, even on a host you granted POSTs to; the ledger is consulted only for
  non-IRREVERSIBLE calls. `fill_secret` / `fill_totp` (credential release),
  `create_login` (identity minting), and the secret-gated `look` are likewise
  excluded — their `scope()` returns `None`, so they always ask.
- **Exact match, no wildcards.** A grant on `boards.greenhouse.com` does not cover
  `api.greenhouse.com`; there is no "all hosts" or "all actions" scope. If the
  page navigates to another host, subsequent actions match the new host and
  re-prompt. The `spawn` grant is still exact-match — on the fixed target
  `"session"` — but because every spawn shares that one key it is deliberately
  session-wide, the only non-host scope.
- **Grants never widen policy.** They short-circuit only the *prompt*, never the
  decision — a `DENY` still denies, and `approve_if` predicates still run.
- **Issuance, use and withdrawal are audited in the hash chain.** Issuance rides
  the approving call's entry (`grant: {id, action_class, target, expires_at}`);
  each covered call records `grant_id`. The agent sees this via `history()`.
- **A grant can be taken back mid-session.** `Broker.revoke_grant(grant_id)`
  drops it and appends `{event: "grant_revoked", grant_id, action_class,
  target}` to the chain. Nothing caches a `Grant` — every dispatch consults the
  ledger fresh — so the very next matching call prompts the human again, or is
  denied outright when no approver is wired. Calls that already ran keep the
  `grant_id` they were audited under: revocation *appends*, it never rewrites
  history, which is the whole point of a tamper-evident log. The reachable
  surface is the Python API (`session.broker.revoke_grant(...)`, with
  `session.broker.grants.active()` to list ids); there is deliberately no CLI
  command and no agent builtin — the ledger is in-memory and session-scoped, so
  a second process has nothing to revoke, and letting the agent revoke its own
  grants is authority the agent does not need. `GrantLedger.revoke()`/`clear()`
  remain as raw, unaudited primitives underneath.
- **Session-lifetime, in-memory.** The ledger dies with the `Session`; nothing
  persists across sessions, so "for this session" is literally true. Standing
  policy has its own home — construct a `Session(policy=…)` without the gate.
  (`GrantLedger.add` takes an optional `ttl_s`; the CLI mints session-lifetime
  grants only. Plan-scoped grants are deferred to the recursive-`spawn` work.)

### The channel model

Two channels connect the agent and the human, and they are deliberately
asymmetric:

- **Inbound (human → agent): the approval prompt, only.** It is the single
  place a human's answer changes what executes, and its text is harness-built
  from the structured call (see above) — never agent-authored.
- **Outbound (agent → human): `notify()`** (`broker/capabilities/notify.py`),
  strictly output-only. The message *is* agent-authored free text, so the
  rendering keeps it from impersonating the harness: the CLI shows it
  standalone under a fixed `[agent note]` prefix, visually distinct from the
  `⚠ approval` prompt and never interleaved into an approval interaction;
  desktop notifications carry a fixed title with the agent's text only in the
  body; and a notification accepts no input anywhere — nothing to click,
  confirm, or reply to. Every notify is a hash-chained audit entry
  (`notify.notify`) like any other capability call.

An agent-authored message can therefore *inform* a decision but never *be* the
decision surface — "reply y to allow" written into a notification has nowhere
to land.

## Egress guard — no requests to the box's own network

The broker runs in the *unsandboxed* parent, so every outbound request the agent
asks for is made from a process that can reach the host's own network. A "free"
GET to `http://169.254.169.254/…` (the cloud-metadata endpoint, link-local on
every major cloud) would hand back the instance's IAM credentials; a GET to a
`localhost` admin port or a Docker socket over HTTP reaches internal services. This
is server-side request forgery (SSRF), and nothing in the policy layer above stops
it on its own.

`pyharness/security/egress.py:check_url` sits on the request path
(`web.fetch` / `http.request` / `browser.goto`, and the remote-MCP transport —
a `.mcp.json` `url` entry is vetted at mount and on every request, so a
config-declared server can't point the parent at an internal endpoint and
forward `secret:` creds in its headers) and, before the request goes out:

- refuses any non-`http(s)` scheme (no `file://`, `chrome://`, `gopher://`); and
- resolves the host and blocks it when it maps to a **link-local** address
  (`169.254.0.0/16`, `fe80::/10`) — the cloud-metadata range, never a legitimate
  fetch target — so a hostname that resolves there (`metadata.google.internal`) is
  caught, not just the bare IP.

The check covers every hop, not just the url the agent named. An HTTP client
that auto-follows redirects would make the initial check worthless — a public
host could 302 to `http://169.254.169.254/…` and the internal body would come
back to the agent — so `http.request`/`web.fetch` never auto-follow: the
capability drives the redirect loop itself, re-running `check_url` on each
`Location` before chasing it (capped at 20 hops so a redirect loop terminates).
The browser closes the same hole at the request layer: every session installs a
Playwright route interceptor that re-vets each network request the page makes —
redirect hops, JS/meta-refresh navigations, subresource fetches — and aborts any
whose target is blocked, and `browser.goto` re-checks where navigation actually
settled before returning page state.

Loopback and RFC1918/ULA private ranges stay reachable by default (local dev, LAN
services, and local MCP-over-http are normal); `PYHARNESS_BLOCK_PRIVATE_NETWORK=true`
extends the block to them for a stricter posture. That default is a deliberate
posture call: pyharness is local-first (the live viewer, local dev servers, and
local MCP-over-http all live on `127.0.0.1`), so blocking private ranges by
default would break the common workflow and train users to disable the guard
wholesale — while the one range that never has a legitimate target, link-local
cloud-metadata, is blocked unconditionally regardless of the flag.

DNS resolution *failure* fails **closed**: a hostname that will not resolve is
refused rather than waved through, so a name that resolves only intermittently
can't slip past the guard on the attempt where our lookup fails.

### Vetting a host, then connecting to it

A check that reads a hostname out of a URL and leaves the client to open the
socket has two seams between the decision and the connection, and an attacker
only needs either one:

- **A second lookup.** The guard resolves the name, the client resolves it
  again, and a resolver under the attacker's control answers differently the
  second time — classic DNS rebinding.
- **A second parse.** The guard and the client each derive a hostname from the
  same string and disagree. The sharpest case is IDNA: CPython's resolver
  applies IDNA 2003 and reads `faß.example.com` as `fass.example.com`, while
  httpx (and every browser) applies IDNA 2008 and reads it as
  `xn--fa-hia.example.com`. One name is vetted; a different one is contacted.

`PinnedTransport` (same module) closes both for every request the harness makes
with httpx — the `http` capability's clients and the remote-MCP transport. It
takes the host from httpx's *own* parse of the request it is about to send, vets
it once, and then **connects to the address it vetted**: the URL's host is
rewritten to that IP while the `Host` header and the TLS `sni_hostname` keep the
original name, so name-based virtual hosting still routes and the certificate is
still verified against the real name. There is no second parse to disagree with
and no second lookup to race. `check_url` stays on the path as the pre-flight
check — it refuses a bad target before a connection is opened or a credential
attached, and its message names which rule fired — but the transport is the
enforcement point. Both attacks are scored: `ssrf-dns-rebinding` and
`ssrf-idna-confusion` in
[the adversarial suite](#the-adversarial-suite--the-claims-attacked).

Two paths stay name-based, and therefore best-effort:

- **The browser.** Playwright owns Chromium's sockets, so nothing here can pin
  them; the route interceptor re-vets every request the page makes instead.
- **A session behind an HTTP(S) proxy.** The socket goes to the proxy, not to
  the address we vetted, so pinning cannot describe the real connection —
  `pinned_client` detects `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` and falls back
  to a plain client with the name-based guard.

Pinning also means a request goes to **one** address (IPv4 preferred when a host
offers both). A host whose first address is down is not retried at a second: a
fetch failure, traded for there being no unvetted address left to reach.

## Host-scoped sessions

`spawn(tools=...)` scopes a child *by capability name*; `allowed_hosts` scopes
*where* the granted network capabilities may reach. A child spawned with
`spawn(task, tools=("web",), allowed_hosts=["api.github.com"])` can only talk
to those hosts and their subdomains — the point is that a child reading
untrusted content (pages can try to steer it) cannot be talked into
exfiltrating to an attacker host, and the human approving the spawn approves a
*bounded* plan (the approval line shows the hosts). `Session(allowed_hosts=...)`
is the general mechanism; spawn is its first user.

Enforcement rides the same egress layer as the SSRF guard, not the policy
layer — a broker-level check sees only the initial URL of a call, while
`check_url(url, allowed_hosts)` runs at every point the guard already covers:
the initial `http.request`/`web.fetch` URL and each redirect hop,
`browser.goto` plus the settled URL, and a remote MCP server's endpoint
(`tools/mcp/transport.py:HttpTransport`) at mount and again per request — a
scoped child holding `tools` (implied by any network grant) cannot
`add_mcp_server(url=...)` its way out of scope. In the browser's route interceptor the
scope applies to **main-frame navigations** (redirects, JS navigation, link
clicks — everywhere the agent can end up); subresource and iframe loads are
scope-exempt (blocking CDNs would break most pages) but stay under the SSRF
guard. Scope entries match a host and its subdomains (`github.com` covers
`api.github.com`); this suffix semantics is deliberate — entries are authored
by the spawning agent and shown to the human, unlike approval grants, which
stay exact-match because they are minted from an observed concrete host.

Under a host scope, `web.search` is refused: the query itself travels
to the search provider (outside any scope), and a free-text query is a classic
exfiltration channel. The scope also does not pre-grant anything: mutating or
secret-carrying calls to in-scope hosts still prompt the human exactly as
unscoped ones do.

Known limits (stated design boundaries, named to the child in its preamble):
exfiltration *to an in-scope host* remains possible — the scope bounds where,
not what, so a permitted host with a free-form field (a search query, an issue
comment, a filename) is a channel that passes every check by construction, and
the payload is deliberately never inspected because that is content filtering
(scored as `scope-abuse-in-scope-channel`, and the fine print on "if the data
matters, scope the session"); subresource/iframe traffic is not scope-bound, and WebSocket
traffic has no interception point at all (`context.route` does not cover WS
and no `route_web_socket` handler exists); capabilities with
fixed off-box targets (`inbox`'s IMAP server, `packages`' index), the
per-command-gated `shell`, and local (command-run) MCP servers are outside
the scope's remit — those stay behind per-call human approval and the OS
sandbox; and the scope rides the same connection pinning as the SSRF rules on
the httpx paths, so it inherits the same two best-effort exceptions — the
browser, and a session behind a proxy.

## Cross-cutting policies are enumerated, not remembered

The 2026-07 security recon found that every verified gap sat at the same seam:
a capability added without being taught about a cross-cutting policy that
already existed. `allowed_hosts` was threaded through some capabilities and
not others; `packages.install` initially skipped the sandbox wrap `shell.bash`
has; the MCP HTTP transport skipped the scope argument. Dispatch is one choke
point, but containment — host scoping, sandbox wrapping, secret-sink
mirroring — is implemented *per capability*, so every new capability is an
opportunity to silently opt out of one.

The structural answer is `tests/test_capability_policies.py`. For each
cross-cutting policy it enumerates every capability **from the live broker
registry** — never from a hand-written list of names — and asserts that each
one either enforces the policy (with the wiring checked on the live instance)
or appears as a named exemption with a written rationale. Registering a new
capability fails those tests until it is classified, in writing, against every
policy: host scoping, parent-side sandbox wrapping, dispatch mediation
(everything the agent can reach is a broker proxy), approval classification
(`preview`/`scope` hooks), and secret-sink wiring.

The exemption tables in that file are the authoritative list of stated design
boundaries. Among them: `shell` and `packages` sit outside the host scope
(per-call approval plus the OS sandbox instead); browser subresource/iframe
loads are scope-exempt and WebSockets have no enforcement point (see above);
local (command-run) MCP servers and the Playwright browser process exec
parent-side outside the sandbox wrap; venv creation and the desktop-notify
helper exec fixed harness-authored argv unwrapped. Each entry carries its
rationale where it is asserted, so a boundary cannot drift from what the tests
actually hold true.

## The adversarial suite — the claims, attacked

Enumeration tests prove the wiring exists. They do not prove it works. `evals/`
holds a scripted adversary that attacks the claims on this page directly — SSRF
against the metadata endpoint in four spellings, host-scope evasion, redirect
escape, credential replay, grant reuse after revocation, log tampering,
delegation escape, MCP rebinding — and scores the result. `make evals` runs it
and rewrites [`evals/SCOREBOARD.md`](../../evals/SCOREBOARD.md), which is
committed; `make test` fails if any attack stops matching what that file says.

Two disciplines make the number worth reading, and both are enforced rather
than trusted:

- **Every attack states its security property without reference to the
  implementation.** A property phrased in terms of the code passes by
  construction. These are claims a sceptical reader could check.
- **An attack never reports "blocked" by merely failing.** It names the
  exception type that constitutes a legitimate refusal *and* the reason string
  that pins which mechanism fired (`EgressBlocked` means both "outside your
  scope" and "DNS failed"), or — for attacks that end in an observation rather
  than a refusal — the independent evidence that the exploit really ran. Anything
  else is an error, reported in its own bucket and never credited to the defense.

The scoreboard reports four counts, not one percentage: blocked, known gaps,
unexpected successes, and errors. The interesting information is entirely in
which bucket a failure landed. Gaps are published with the reason each is a
stated boundary — most of them are also asserted, with the same rationale, in
the exemption tables above. Regression is bidirectional: a gap that gets closed
fails the suite too, so this page cannot quietly go stale in either direction.

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
`text`, or `headers`. Masking covers the secret's URL-encoded spellings too, so a
`"query"` secret that comes back percent-encoded in the url (`p@ss` -> `p%40ss`)
is still caught. A resolved secret never round-trips through agent-visible output.

**Masking is a backstop, not the boundary.** `redact` is a literal substring
replace over the raw value and its URL-quoted forms; it catches a verbatim or
percent-encoded echo but *not* a value a server re-encodes before echoing it —
base64/hex, HTML entities, JSON/unicode escapes, or a value split across chunks
all pass through. The real containment is the two rules above (a resolved secret
only ever travels to a vetted host, and never enters agent-visible output by
design); masking cleans up the incidental echo and is deliberately not relied on
as the perimeter. Widening the encodings covered would trade a few more catches
for `***` false positives on innocent text, so it is intentionally left narrow.

Masking covers the **exception path** too, as a defense-in-depth invariant. An
exception is a perfectly good exfiltration envelope — an `httpx` error's repr
embeds the full request URL (query params included), `TimeoutExpired` the whole
argv — and success-path masking inside a capability never sees it. So every
per-context sink mirrors its masks into one session-wide `SecretSink`
(`Session.secret_sink`), and three surfaces redact through it: the broker masks
the audited `repr(exc)` of a failing call (so `audit.jsonl`, the trace, and
telemetry never carry cleartext), the in-process kernel masks the returned cell
output (traceback included), and the out-of-process host rewrites a
secret-bearing exception as a `RemoteError` with a masked message *before* it
crosses the pipe — cleartext never even enters the child process. Clean,
secret-free exceptions pass through untouched, type intact, so agent code can
still catch them; the redaction runs only on error paths and is a no-op while no
secret has been resolved.

The sink is also where **host binding** is enforced. A vault entry can be bound
to the host(s) it belongs to at config time (`pyharness-vault set github --host
api.github.com`); every injection surface passes the concrete destination — the
request URL's host, the browser page's host, the IMAP server — into
`SecretSink.resolve`, which refuses a bound secret toward any other host before
anything goes out. Sending a bound credential to the wrong host is thereby
*impossible*, not merely visible in the approval prompt; the prompt remains the
check for unbound secrets. Matching is exact per hostname, case-insensitive, no
wildcards — the same shape as grant scopes. (A secret-carrying HTTP request
never follows redirects, so the checked host is the only one the credential can
reach.)

The same rule covers values *derived* from a secret. A TOTP seed is a plain
vault secret (by convention `<site>_totp`); `browser.fill_totp` resolves it
parent-side, derives the current RFC 6238 code (`security/totp.py`, stdlib
only), types the code in, and registers the code with the sink too — so a page
echoing "you entered 287082" comes back masked. Neither the seed nor the code
has a code path back to agent code: there is deliberately no code-returning
surface, because a single exception would make the no-cleartext rule
negotiable. This is what makes an unattended re-login on a 2FA site possible —
the human approves the `fill_totp` action, not the code.

Masking works on text; pixels it cannot reach. A secret typed into a page is
visible in a screenshot, so both `browser.look` (which puts a screenshot in the
model's context) and `browser.screenshot` (which writes a PNG the agent can read
back from the workspace) are gated by the default policy once a session has
injected a secret — an argument-dependent `approve_if` predicate, the same
mechanism that gates state-changing HTTP by method. Either way the value never
renders on the masked `http` path, so that remains the safest place for credential
entry.

Resolution order, first hit wins: in-memory dict → environment
(`PYHARNESS_SECRET_<NAME>`) → encrypted file. The file is sealed with a
passphrase-derived key (scrypt) and Fernet (authenticated AES); a wrong
passphrase fails to decrypt rather than returning garbage, and is reported as a
`VaultPassphraseError` naming the variable rather than Fernet's bare
`InvalidToken` — an unexplained crypto failure reaching agent code is one the
model will invent a cause for when it answers the human. The interactive CLI
checks a prompted passphrase against the sealed file before the session starts,
so the common case (a typo) never gets that far. See
[Use the secrets vault](../how-to/use-the-vault.md).

## Site profiles — a login the agent can name but never read

A browser context dies with the `Session`, so without persistence every task on
an authenticated site pays login + 2FA again — a human in the loop every time.
`pyharness/security/profiles.py:ProfileStore` fixes that by saving a page's
`storage_state` (cookies + localStorage) under a name, sealed with the **same**
scrypt+Fernet envelope as the vault (same `PYHARNESS_VAULT_PASSPHRASE`), one file
per profile at `~/.pyharness/profiles/<name>.enc` (override
`PYHARNESS_PROFILES_DIR`). Cookie material is credential-grade, so it lives under
the same **use-but-don't-view** rule as secrets:

- The cleartext `storage_state` is a dict that never leaves parent memory — it is
  never written to disk unencrypted (Playwright round-trips it in-memory, no temp
  file) and never returned to agent code. `open_browser(profile=...)`,
  `save_profile`, and `list_profiles` hand back only a session id, counts, or
  names. The profile *name* is the sole agent-supplied input and is regex-validated
  in one place, so it can never become a path.
- **No passphrase → fail closed.** With no `PYHARNESS_VAULT_PASSPHRASE`,
  `ProfileStore.from_env()` returns `None` and opening or saving a profile raises;
  there is no plaintext fallback.
- **Two credential-moving ops always prompt** under the default policy and are
  never grant-coverable: `open_browser(profile=...)` (category OUTWARD — it opens
  the browser *as that identity*, and it is the last checkpoint before free `goto`
  transmits the restored cookies) via an `approve_if` predicate, and `save_profile`
  (writes a standing credential) via `require_approval`. `fill_secret`,
  `fill_totp`, and the secret-gated `look` are unaffected.
- **Refresh on close is audited, not prompted.** A profile-opened session re-saves
  its (rotated) state when it closes so the login survives; because close does not
  flow through the broker, the capability records a `profile_saved` event straight
  into the hash chain. Profile *deletion* is human-only (a CLI action, not an agent
  builtin).

The honest boundary: a profile session gives agent code the *powers* of the
logged-in identity (a free `read_text` can read your inbox) but never the
credential *bytes* — and the powers are exactly what the one open-approval signs
off. Two accepted residuals, documented rather than hidden: auto-refresh persists
*every* cookie the context accrued (so keep a profile session on its site), and a
minority of sites store auth in `sessionStorage`, which `storage_state` does not
capture.

## The out-of-process sandbox

When the agent runs in a child process (the default — the in-process kernel
requires the explicit, test-only `unsafe_in_process=True` opt-in), two OS-level
layers confine it (`pyharness/broker/remote/sandbox.py`), on top of the process
boundary itself:

- **Minimal environment (default-deny)** — the child's environment is reduced to
  a small allowlist (`security/env.py`: PATH/HOME/locale/TLS-trust/proxy basics)
  before any agent code runs, and any subprocess the child spawns inherits the
  reduced copy. Default-deny rather than a denylist: the harness's own secrets
  (`PYHARNESS_SECRET_*`, the vault passphrase, the provider API keys — e.g.
  `ANTHROPIC_API_KEY`) are gone, and so is anything *else* a user put in `.env` —
  an `AWS_SECRET_ACCESS_KEY` or `DATABASE_URL` never has to be known about to be
  kept out. The child has no LLM client of its own (completions route through the
  broker), so it needs none of them, and even `printenv` reads nothing the parent
  holds. The same minimal environment seeds the two other subprocess-spawn paths
  that would otherwise inherit the parent's environment wholesale: `shell.bash`,
  and a **local (stdio) MCP server** — arbitrary third-party code, so it gets the
  allowlist with only its own configured `env` (resolved `secret:` refs) layered
  back on. `PYHARNESS_ENV_PASSTHROUGH="FOO,BAR"` admits extra vars a workflow
  genuinely needs; it can never resurrect the harness's own secret variables.
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
  the profile. **`shell.bash` runs under this same profile** — the command
  executes in the (unsandboxed) parent, so it is wrapped in `sandbox-exec` with
  the identical Seatbelt profile (`sandboxed_shell_argv`) rather than trusted
  with the parent's OS reach; the generated profile lives outside the workspace,
  so a sandboxed command can't rewrite its own jail. On Linux the same function
  returns a launcher that applies the confinement and then `exec`s the command —
  `bash` runs parent-side and so cannot restrict itself. Where the platform has
  no OS sandbox, `bash` falls back to the scrubbed environment alone (and remains
  approval-gated).
- **`packages.install` runs pip under its own profile.** A package's `setup.py`
  or build hook is arbitrary code executing at install time, in the privileged
  parent, so it is wrapped for the same reason `bash` is. Its profile differs in
  exactly one way — **outbound network is allowed**, because pip has to reach the
  index — while the `$HOME` read jail stays and writes are confined to the
  session venv plus a dedicated scratch dir. Notably *not* the whole sandbox dir:
  the generated profiles live there, so a build hook able to write it could
  rewrite the jail confining the next child. pip is also given
  `PIP_NO_CACHE_DIR=1` and a `TMPDIR` inside the scratch dir, since its usual
  `$HOME` cache is on the far side of the read jail.
- **POSIX rlimits** — no core dumps; on Linux, a process cap to blunt fork bombs
  (skipped on macOS, where the limit is per-user and would break ordinary
  `subprocess`/`fork`).

The child needs no network and no writes outside its workspace: every side
effect that leaves the box goes back through the broker in the parent, while
scratch files stay in the workspace where both the agent and the human can see
them.

### The Linux backend — Landlock plus seccomp

Linux enforces the same three invariants through different machinery
(`broker/remote/linux_sandbox.py`):

- **Landlock** (the kernel's unprivileged filesystem LSM) supplies the write jail
  and the `$HOME` read jail — a ruleset naming the readable and writable
  subtrees, with everything unnamed denied.
- **A seccomp-bpf filter** denies `socket(2)` for `AF_INET`, `AF_INET6` and
  `AF_PACKET`, which is the network denial. Landlock's own network support
  restricts TCP bind/connect by port only and leaves UDP open, so it cannot
  express "no outbound network"; keying on the address family covers TCP, UDP and
  raw in one rule.

Both are applied by the process to itself — no helper binary, no user
namespaces, no root — and both are irrevocable and inherited across `exec`, so a
subprocess the agent spawns cannot escape either. It follows that this works
inside an ordinary container, which bubblewrap (the obvious alternative) does
not: bubblewrap needs unprivileged user namespaces, which Docker's default
seccomp profile blocks and which Ubuntu 24.04 LTS blocks out of the box.

One consequence worth knowing, because it has no macOS analogue: the broker
parent holds your API key and vault passphrase in its environment and runs as the
same user, so `/proc/<parent>/environ` would ordinarily be readable by the child.
Landlock also hooks `ptrace_access_check`, and a restricted process may not
inspect a less-restricted one — so that read is denied, as is `PTRACE_ATTACH`
against the parent.

**The floor is Landlock ABI 3 (Linux 6.2)**, on x86-64 or arm64. ABI 3 is the
first with `LANDLOCK_ACCESS_FS_TRUNCATE`; below it, `truncate(2)` on an
already-open descriptor escapes the write jail, so older kernels report *no
sandbox* rather than a jail with a hole. That covers Ubuntu 24.04 LTS and
Debian 13, and excludes Ubuntu 22.04 and Debian 12.

One structural difference to keep in mind when editing the profile: **Seatbelt is
a denylist and Landlock is an allowlist.** The macOS profile enumerates what is
forbidden; the Linux one must enumerate everything the child legitimately reads,
including the per-session venv (which lives outside the workspace). Omitting a
path there does not weaken confinement — it breaks an import at runtime.

### What the sandbox does not cover — other platforms

Confinement is built for macOS and Linux. **Windows has neither** — no network
denial, no write jail, no `$HOME` read jail, and not even the rlimits above;
agent code would run with your user's full filesystem and network reach, kept
honest only by the process boundary and the minimal environment. The same is
true of a Linux kernel below the ABI floor.

That absence is loud, not silent. On a platform with no OS sandbox, pyharness
**refuses to start a kernel by default**
(`broker/remote/sandbox.py:check_unsandboxed_platform`, raised when the session
constructs its out-of-process kernel — before any agent code or LLM spend).
Running anyway requires the explicit `PYHARNESS_ALLOW_UNSANDBOXED=true` opt-in,
which prints a one-time stderr warning that agent code is unconfined. On macOS
the gate is a no-op and the variable is ignored — the sandbox is always on.
`shell.bash` composes with this: its no-sandbox fallback (env scrubbing only,
still approval-gated per command) is only reachable behind the same opt-in,
because without it the session never starts.

> Workspace-internal writes are deliberately *not* individually audited — the
> audit chain records effects that cross the perimeter, not every `savefig`. The
> workspace is inspectable on disk; the broker is where outward actions are gated
> and logged.
>
> **The same rule covers local execution.** A cell that calls `subprocess.run`,
> `os.execv` or `ctypes` runs a program inside the jail without an audit record,
> exactly as a cell computing in-process does. That is the perimeter rule applied
> consistently, not an oversight — and it is also the only option available:
> hooking `subprocess` in the child would be advisory, since agent code owns that
> process and can reach `posix_spawn` or libc directly. The audit chain is
> trustworthy *because* it lives in the parent, on the far side of the IPC
> boundary, recording calls that arrive there. Anything such a subprocess tries
> to do outward — network, writes outside the workspace, reads of `$HOME` — is
> denied by the OS sandbox it inherits, which
> `test_remote_kernel.py::test_sandbox_is_inherited_by_a_subprocess_agent_code_spawns`
> and `test_linux_sandbox.py::test_no_escape_by_exec` assert directly.

## Audit — a tamper-evident record

`pyharness/audit.py` appends every capability call to `audit.jsonl` as a hash
chain: each entry stores `hash = sha256(prev_hash + entry)` and a `prev` pointer.
Any later edit, deletion, or reordering *within the file* breaks the chain, so
the log is verifiable — which matters once it's shipped off-box.

A bare chain from an empty genesis has two blind spots: deleting the last N
entries (**tail truncation**) leaves a shorter chain that still verifies, and
anyone who can rewrite the whole file can re-link a **forged chain** from
scratch. To raise that bar each session keeps a small **anchor** sidecar
(`audit.jsonl.anchor`) with the current entry count and head hash, written
atomically as the chain advances; `verify_chain` cross-checks it, so a naive
truncation (count no longer matches) or full rewrite (head no longer matches) of
the log *alone* is caught. It is not a keyed MAC and makes no such claim: an
attacker who can rewrite *both* the log and its anchor can still produce a
consistent pair, and a deleted anchor falls back to the chain-only verdict (it
can't be told apart from a legacy log that predates the anchor). A real
cryptographic trust root needs a key store this harness doesn't have; the anchor
is a pragmatic bar against casual tampering, not a guarantee against a determined
one.

Each call writes **two chained records**, not one: an *intent* record
(`phase: "start"`, the action plus its summarized args) before anything runs —
before even the policy check, so denials and refused approvals carry it too —
and an *outcome* record (`phase: "end"`) on every exit path: success, capability
error, policy deny, refused approval. Approval decisions that let the call
proceed (a human's yes, or a covering grant) sit between the two as their own
records, as before. The point of the split is actions that never complete: an
action killed mid-flight used to vanish from the log entirely (it could run for
minutes and spend real money, unrecorded); now its start is already in the
chain, and session teardown best-effort appends the outcome record
(`ok: false, error: "aborted"`) for anything still executing. A start with no
outcome record at all means the process died too hard even for that — the
digest reports it as `aborted_actions`. Chain verification is unaffected: the
verifier checks the hash chain, not record pairing, so both records verify like
any others.

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
