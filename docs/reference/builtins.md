# Builtins

The functions always in scope inside a `run_python` cell. Call them **directly by
bare name** — never import them. This list is the whole set; anything else is a
[tool](../how-to/add-a-tool-or-skill.md) you load on demand. Paths are relative
to the session workspace.

This is the authoritative contract the orchestrator is given (see
`SYSTEM_PROMPT` in `pyharness/core/agent.py`). Each turn a small dynamic
**session block** is appended to that static prompt — the current date/time and
zone, the platform, and the workspace path (`render_context` in the same
module), plus, when a [session index](../how-to/observability.md#the-session-index)
is configured, a few ambient lines of the agent's own past: recent sessions
(name, task, outcome, cost), skill trust states, and established lessons — so
the model starts oriented in its history instead of having to remember to look.

The harness also manages the agent's context for it. Each cell's result ends
with a one-line meter — `[context: N tokens · step i/max · spent $…]` — so
context pressure and spend are facts the model sees, and outputs of cells older
than the most recent few (`PYHARNESS_KEEP_OUTPUTS`, default 8; see
[Configuration](configuration.md)) are elided to a short
`[output elided: …]` stub. Elision is safe here in a way it isn't in most
harnesses: the kernel is persistent, so any elided output is one `print()`
away, and the full text remains in `trace.jsonl`.

## Files & shell

| Signature | Returns |
|-----------|---------|
| `read(path, offset=0, limit=None)` | file contents (whole, or a line window) |
| `write(path, content)` | — (creates/overwrites) |
| `edit(path, old, new)` | — (replaces `old` with `new`) |
| `bash(cmd, timeout=60)` | combined stdout/stderr — **needs human approval** |
| `search(pattern, path=".")` | matching lines |

`read` returns the file whole by default; `offset`/`limit` page it by line
(skip `offset` lines, return at most `limit`) instead of pulling a long file
into context. None of these cap their result — the content lands in a kernel
variable intact; the only cap is on what the agent chooses to `print()` back to
itself (see [the action space](../explanation/action-space.md#large-and-binary-payloads)).
`bash` needs human approval per call, runs with secret-bearing env vars
stripped, and on macOS executes inside the same OS sandbox as the out-of-process
child: no outbound network, writes confined to the workspace, `$HOME` reads
jailed. Reach the network through the `web`/`http` tools, not `curl`. (See
[Security & audit](../explanation/security-and-audit.md).)

## Reaching the outside world is not a builtin

Everything that reaches an external system — the web, a browser, HTTP APIs, the
package index, MCP servers, learned skills — is a **tool**, not a builtin. None
are in scope by default; the agent discovers and loads them the same way
(`search_tools` → `describe_tool` → `use_tool`), and every call is gated exactly
as a builtin's is. The line: builtins are the agent's own body; tools are what it
reaches out to. The first-party external tools ship registered under the `web`,
`email`, and `packages` categories — find them with `search_tools("web")` /
`search_tools("email")` / `search_tools("install")`:

| Tool | `search_tools` | What it is |
|------|----------------|------------|
| `web` | `web` | `search` (a raw ranked list to fan out over — each item `{title, url, snippet, published_date, author, score}`; backed by Exa, needs `EXA_API_KEY`, its per-query cost is not metered) + `fetch` (one-shot GET returning a readable page map — HTML reduced to clean markdown content plus `## FORMS` (each form's action/method and every field) and `## LINKS` (navigable links, resolved to absolute URLs) so the agent can see what to click and fill; non-HTML verbatim; static HTML only, JS-rendered affordances need `browser` — a page whose extraction looks thin (almost no body text but 20+ links, the shape of a JS-rendered shell) gets a single `[warning: extraction looks thin …]` line prepended to the map. A thin wrapper over `http.request`; `save="path"` or a binary body writes to the workspace and returns a note pointing at the file, still carrying the FORMS/LINKS map) |
| `http` | `web`, `http`, `api` | Stateful HTTP: `open_session` (cookies persist on the id across cells), `request` (returns `{status, url, headers, content_type, elapsed_ms, title, links, forms, text, path, bytes, preview, saved}` — `title`/`links`/`forms` are the parsed affordances, populated for HTML and riding inline even when the body spills to disk), `close_session`. POST/PUT bodies, multipart upload of a workspace file, named-secret injection |
| `browser` | `web`, `browser` | Headless Playwright lane: `open_browser` (pass `profile="name"` to restore a saved login — needs approval, see below) / `goto` / `snapshot` (accessibility tree with stable `[ref=eN]` handles per element, links carrying their url) / `click` / `fill` / `fill_secret` / `fill_totp` (types the current 2FA code derived from a vault TOTP seed — see below) / `select_option` / `press` / `upload` (each targets a `ref=` from the last snapshot or a CSS/text `selector`) / `scroll` / `wait_for` (returns `{found: bool}`; a timeout is a clean `False`) / `read_text` / `look` (a JPEG screenshot delivered to the model as an image it sees — gated once a secret was typed into the page) / `screenshot` (writes a PNG to the workspace — gated the same way once a secret was typed) / `save_profile` (persist this browser's login as an encrypted profile — needs approval) / `list_profiles` (names only, free) / `close_browser`. Host-provisioned: this lane runs host-side, so it needs `make browser` (the `pyharness[browser]` extra + `playwright install chromium`) on the host — an in-session `packages.install` cannot enable it |
| `inbox` | `email` | Read-only email over IMAP, one account: `list` (newest-first metadata rows `{id, from, to, subject, date, seen, has_attachments}` — never bodies) / `search` (server-side; `query`/`from_`/`subject`/`since` AND together, same metadata rows) / `read` (one message: headers, the clean text body, a `links` list collecting HTML anchors and bare URLs — a verification/magic link is a first-class value to hand to `web.fetch` or the browser — and attachments written to `inbox/<folder>/<id>/` in the workspace, never inline). Structurally mutation-free: no send/delete/flag/move op exists, fetches are `BODY.PEEK` on a read-only folder, so the mailbox (including unread flags) is left untouched. Config: `PYHARNESS_IMAP_HOST`/`PORT`/`USER` + the vault secret `imap` (see [configuration](configuration.md#email-inbox)) |
| `packages` | `install` | `install` a PyPI package into the session venv for later `import`; `list_installed` lists what's already there |

`describe_tool(name)` is the live source for each tool's exact signatures — the
docs don't duplicate them. The non-inferable semantics that survive the move:

- **Reads are free; state-changing calls need human approval.** GET/HEAD and page
  reads run unattended; POST/PUT/PATCH/DELETE and `click`/`fill`/`fill_secret`/
  `fill_totp` are gated per call. This holds whether the capability is a builtin
  or a tool.
- **Bodies come back whole — inline or on disk.** A textual response (and
  `browser.read_text`) rides back as `text`, uncapped, for the kernel to parse.
  A binary body, a response past the inline ceiling, or an explicit `save="path"`
  is written to the workspace instead: `text` is `None` and `path`/`bytes`/`preview`
  point at the file, which the agent reads with its own Python. Never a truncated
  head. See [the action space](../explanation/action-space.md#large-and-binary-payloads).
- **Secrets never round-trip through agent-visible text.** `auth`/`secret_fields`
  (http) and `fill_secret` (browser) name a vault secret resolved parent-side;
  the value is masked (`***`) out of every returned `url`/`text`/`headers`, out
  of `read_text`, and out of the `snapshot` tree. `fill_totp` extends this to
  2FA: the vault holds a TOTP *seed* (a plain secret, by convention named
  `<site>_totp`), the current RFC 6238 code is derived parent-side at the moment
  of use and typed in, and both seed and code are masked from every read-back.
  Like `fill_secret`, it is never covered by a grant — it prompts every time.
  Attaching any secret to a request (`auth`/`secret_fields`) needs approval
  regardless of method — a credential going off-box is gated even on an otherwise
  free `GET` — and such a request does not follow redirects, so a credential can't
  be resent to a cross-origin `Location`. Pixels are the one channel redaction
  can't reach, so both `look` (puts the image in the model's context) and
  `screenshot` (writes a PNG the agent can read back) are gated once this session
  has typed a secret into the page.
- **`look` is the one non-text channel back to the model.** Every other result is
  text; `look` attaches a JPEG screenshot to the call's result as an image block
  the model actually sees (for a chart, a rendered PDF, a layout, a CAPTCHA the
  page shows). It stays in history and costs context on every later turn, so
  prefer `snapshot` for structure and reach for `look` only when you need pixels.
- **See the page before acting on it — `browser.snapshot` then act by `ref`.**
  The snapshot is an accessibility tree where every element has a stable
  `[ref=eN]` handle; `click`/`fill`/`fill_secret` take that `ref=` (or a CSS/text
  `selector`), so the agent acts on elements it has actually seen instead of
  guessing selectors. Refs are valid only against the most recent snapshot:
  `goto` invalidates them (re-snapshot on the new page), and an unknown or stale
  ref is rejected immediately with a clear message rather than after a locator
  timeout. Its body follows the same whole-or-on-disk rule as `read_text`.
- **Stay logged in across sessions — named profiles.** `list_profiles()` shows
  saved logins; `open_browser(profile="x")` opens already authenticated (skipping
  login + 2FA), and after logging in fresh, `save_profile(session_id, "x")` persists
  it. Cookies are encrypted at rest and never returned to agent code; opening or
  saving a profile needs approval. See
  [Keep the agent logged in](../how-to/site-profiles.md).
- **Prefer the `http` path over `browser` for sensitive credentials** — the
  browser DOM is agent-readable, so it is lower-assurance.
- Live handles (the `httpx.Client`, the Playwright page) stay parent-side, keyed
  by the session id the agent holds; state persists across cells.

See [Add a tool or save a skill](../how-to/add-a-tool-or-skill.md) and
[Security & audit](../explanation/security-and-audit.md).

## Credentials

| Signature | Returns |
|-----------|---------|
| `secrets() -> list[str]` | names of secrets you may reference — **never** the values |
| `create_login(site, length=20, symbols=True) -> dict` | mint a signup identity for a site — **requires approval, per site (never grant-covered)**. Derives a per-site email from `PYHARNESS_IDENTITY_EMAIL` (`local+<host>@domain`) and generates a password parent-side, storing both in the vault bound to the site's host. Returns `{host, email, email_secret, password_secret, created, password_length}` — the email in clear (type it with `fill`), the password only as a vault name for `fill_secret`; its value is never obtainable. `length` (12–64, floor enforced) and `symbols` (`True`/`False`/a string of allowed punctuation) fit site password policies. Existing entries are never overwritten: a repeat call returns the same names with `created=False` |

See [Use the secrets vault](../how-to/use-the-vault.md).

## Delegation

LLM calls as functions: digest or transform data the agent holds without that
data ever entering the orchestrator's context. Workers are one-shot and
toolless — they cannot call capabilities or run code.

| Signature | Notes |
|-----------|-------|
| `llm(prompt, tier=None, system=None, context=None, max_tokens=None) -> str` | one completion; `context` is a string appended to the prompt — the way to hand a large variable to a worker instead of printing it. `max_tokens` bounds the answer; it must be a positive integer at or below the tier's output ceiling (32000 smart / 16000 mid / 8000 cheap) — `0`/negative or a value above the ceiling raises `ValueError` (in `llm()` it propagates; in `map_llm` it becomes a per-task `Result` error). Wall-clock bounded (`WORKER_TOTAL_DEADLINE_S`, 600s, retries included) so one call can't block the kernel indefinitely; long or retrying calls emit streaming/retry heartbeats to the viewer |
| `map_llm(prompts, tier=None, system=None, context=None, contexts=None, max_concurrency=8, max_tokens=None) -> list[Result]` | parallel fan-out of `llm()`; each `Result` has `.ok`, `.value`, `.error`; a failed worker becomes data, not an exception. `.ok` is transport-level only — a worker's refusal comes back `ok=True` and must be filtered semantically. `contexts` pairs one context per prompt (must match `prompts` in length; mutually exclusive with scalar `context`, which applies to all). `system` defaults to a fixed return-only-the-result worker prompt. Count-capped (`max_per_call=64` per call, `session_cap=256` per session in `pyharness/broker/capabilities/llm.py`) |
| `spawn(task, tools=("web","http"), budget_usd=None, max_steps=15, tier="mid", allowed_hosts=None) -> str` | start a real sub-agent: a scoped child session — own kernel, context, and step/budget walls — that works the task to completion **in the background**. Returns a handle immediately; collect the report with `wait()`. **Needs human approval** (the prompt shows the task, capability set, host scope, and budget slice — approving it approves the child's whole plan) |
| `wait(handles=None, timeout=None) -> SpawnResult \| list[SpawnResult]` | block until spawned children finish and return their reports — a single `SpawnResult` for one handle, a list (in handle order) for a list, every child so far for `None`. On `timeout` (seconds) raises `TimeoutError`; the children keep running and a later `wait()` still collects them |
| `spawn_status() -> list[dict]` | one row per spawned child: `session` (the handle), `state` (`"running"`/`"done"`), `spent_usd` — the cheap glance while children work in the background |

`spawn` specifics: children run in parent-side threads, so several can run in
parallel — start them in one cell, keep working, `wait()` when the reports are
needed. The child always holds its body (`read`/`write`/`edit`,
`search`, `llm`/`map_llm`); `tools` grants more by name from `shell`,
`secrets`, `skills`, `history`, `obs`, `notify`, `web`, `http`, `browser`,
`inbox`, `packages` (granting an external one implies tool discovery). It can
never spawn — delegation depth is one by construction.
`allowed_hosts=["api.example.com", ...]` additionally confines the child's
`web`/`http`/`browser` reach — and any remote (HTTP) MCP server it mounts —
to those hosts and their subdomains: anything else is refused at the egress
layer (including redirect hops and browser navigations), and `web.search` is
disabled under a scope (the query would leave it); requires a network
capability in `tools`. `shell`, `packages`, and local (command-run) MCP
servers are not host-scoped — they stay behind per-call approval and the OS
sandbox. See
[security](../explanation/security-and-audit.md#host-scoped-sessions). It shares the parent's
workspace (file handoff is free; the parent assigns output paths in the task)
and starts with none of the parent's conversation, so the task must be
self-contained: objective, output format, boundaries, paths. Its own approvals
bubble to the same human, labeled with the child's name; its side effects land
in the parent's audit chain; its session dir is a sibling
(`<parent>-spawn-NN` — also the handle), so
`inspect_session(result.session, question)` answers follow-ups without
re-reading its transcript. `SpawnResult` fields: `.ok`, `.report` (the child's
final message, verbatim), `.outcome` (the shared session vocabulary),
`.session`, `.spent_usd`, `.steps`. A failed child comes back as a
`SpawnResult` with `ok=False`, not an exception. When the parent session
closes with children still running, they are stopped cooperatively (their
budget slice drops to what they spent, ending them at the next step boundary).
Per-session spawn cap: 16 (`pyharness/broker/capabilities/spawn.py`).

`tier` is one of `"smart"` / `"mid"` / `"cheap"` and defaults to **`"cheap"`**
when omitted — pass `tier="smart"` explicitly for hard reasoning. Tiers map to
models in `pyharness/llm/client.py`: `smart` → Opus, `mid` → Sonnet, `cheap` →
Haiku. See [Budget](../explanation/budget.md).

## Tool discovery

Find a tool, inspect it, then load and call it.

| Signature | Returns |
|-----------|---------|
| `search_tools(query="", include_all=False) -> str` | ranked **headers** (name, summary, source/category); search by what you need (e.g. `"web"`), `include_all=True` or `"*"` lists the whole catalog |
| `describe_tool(name) -> str` | that tool's functions (signatures + docstrings); for a learned skill, also its instructions |
| `use_tool(name) -> module` | load it, then call its functions on the returned module |
| `add_mcp_server(name, command=None, args=(), url=None, env=None, headers=None, summary=None, keywords=(), category=None, timeout=30.0, save=False) -> str` | mount an MCP server (local `command` or remote `url`) as a tool named `name`; **requires approval**. Credentials go as `"secret:NAME"` vault refs. `summary`/`keywords`/`category` feed `search_tools` ranking; `timeout` (seconds) bounds each request. `save=True` persists it to the session's MCP config for later sessions (refuses cleartext env/header values) |

MCP tool calls made through a loaded module are broker-gated per call: reads
declared `readOnlyHint` flow, anything else prompts (grantable per server), and
a declared `destructiveHint` always re-asks — see
[the approval policy](../explanation/security-and-audit.md).

## Skills

Package a repeatable procedure so this and later sessions can reuse it.

```python
save_skill(name, description, instructions, files=None, keywords=(), category=None, check=None) -> str
edit_skill(name, edits, reason="") -> str
record_skill_use(name, outcome, note="") -> str
```

`instructions` is the markdown how-to; `files` is `{"helper.py": source, ...}` of
optional bundled modules. Persists to disk and registers as a learned tool. See
[Add a tool or save a skill](../how-to/add-a-tool-or-skill.md).

> Saving or editing a skill requires human approval by default (it writes
> content that auto-loads in later sessions) — see
> [the approval policy](../explanation/security-and-audit.md).

**Every skill should carry a `check`** — one line saying how a run confirms the
skill worked (an assertion, a re-fetch, an expected state). It is stored in the
SKILL.md frontmatter and shown by `describe_tool` above the instructions, so
`record_skill_use` outcomes rest on evidence, not the runner's impression.

**Trust is earned, not asserted.** A newly saved or revised skill is
**unverified** — it has never run successfully, so its steps are a hypothesis.
`record_skill_use(name, outcome, note="")` logs how a run went (`outcome` is
`"worked"` or `"failed"`); the first `"worked"` marks the skill `verified`. The
log is a bounded per-skill journal (`journal.json` beside `SKILL.md`) so a later
session sees how it last behaved. `search_tools` tags an `unverified` or
`last-failed` skill; `describe_tool` shows the verification state and recent uses
above the instructions. Recording a use writes only metadata, so it is *not*
gated for approval.

**Revising a skill: prefer `edit_skill`.** `edits` is a list of
`{"old": <exact text occurring once in the instructions>, "new": <replacement>}`
deltas — surgical fixes that keep every detail not being corrected (a wholesale
regeneration is how accumulated procedure knowledge gets destroyed). Frontmatter
and bundled files are untouched; the revision resets to unverified while keeping
the use log. `save_skill` with the same name still fully replaces a skill (stale
bundled `.py` are dropped) when a rewrite is genuinely intended.

## Reflection

Read your own action record — the observe half of `do → observe → revise`.

| Signature | Returns |
|-----------|---------|
| `history(limit=20, action=None) -> list[dict]` | your recent actions, oldest last: `{ts, action, decision?, ok?, args?, error?}`. `action` filters by prefix (`"http"`, `"browser.click"`) |
| `stats(sql=None, limit=200) -> list[dict] \| str` | read-only SQL over the [session index](../how-to/observability.md#the-session-index) — every *past* session, LLM call, capability call, error, and skill use, plus aggregate views. No `sql` → the schema reference |
| `inspect_session(session, question) -> str` | a targeted answer about one past session: a cheap-model worker reads that session's full transcript and returns the answer. `session` is a name from `stats()` or the preamble |

`history()` is this session's action record: the audit log lives at the session
root, outside the workspace the file builtins are confined to, so this is the
only way agent code reaches it. It records *side effects* — every capability
call, its decision (allow/approve/deny), whether it succeeded, and a secret-safe
summary of what was sent — so you can confirm an effect landed (`request`
returned 200), see why an action was refused, and revise from what actually
happened. Your own cells and notes are already in context, so only the audit is
surfaced. See [Security & audit](../explanation/security-and-audit.md).

`stats()`/`inspect_session()` are the cross-session half — **query the past,
don't carry it**: aggregate questions ("how reliable is this skill", "what keeps
failing on this host", "what did similar tasks cost") go to SQL; a targeted
question about one run goes to a disposable worker so the transcript never
enters the orchestrator's context. Both are dataless unless the session was
given an index (`Session(index_db=...)`; the CLI wires it by default) — see
[Observability](../how-to/observability.md). `stats` SQL runs on a read-only
connection that cannot `ATTACH` other files; `inspect_session`'s worker call is
metered against the session budget like any delegation.

## Reaching the human

| Signature | Returns |
|-----------|---------|
| `notify(message, level="info")` | `"delivered"` — a one-way note to the user |

The agent's one **outbound** channel to the human, for use from inside a running
cell: a checkpoint worth knowing (`"info"`), blocked / needs a human soon
(`"attention"`), or long work finished (`"done"`). The message is shown live in
the CLI under a fixed `[agent note]` prefix, recorded in the trace and the
hash-chained audit, and mirrored best-effort as a desktop notification (macOS
`osascript` / Linux `notify-send`; silently skipped where unavailable, with the
agent's text only ever in the body under a fixed title).

Strictly output-only: a notification carries no interactivity — nothing to
click, confirm, or reply to — and is rendered unmistakably as agent-authored
text, so it can never pass as an approval prompt. The approval prompt remains
the only channel that accepts human input (see
[Security & audit](../explanation/security-and-audit.md#the-channel-model)).
The prompt teaches restraint: checkpoints and attention-worthy events, never
narration — the plain-text reply remains the answer channel.
