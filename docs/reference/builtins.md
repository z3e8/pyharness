# Builtins

The functions always in scope inside a `run_python` cell. Call them **directly by
bare name** — never import them. This list is the whole set; anything else is a
[tool](../how-to/add-a-tool-or-skill.md) you load on demand. Paths are relative
to the session workspace.

This is the authoritative contract the orchestrator is given (see
`SYSTEM_PROMPT` in `pyharness/core/agent.py`). Each turn a small dynamic
**session block** is appended to that static prompt — the current date/time and
zone, the platform, and the workspace path — so the model has the world-state it
would otherwise burn turns discovering (`render_context` in the same module).

## Files & shell

| Signature | Returns |
|-----------|---------|
| `read(path, offset=0, limit=None)` | file contents (whole, or a line window) |
| `write(path, content)` | — (creates/overwrites) |
| `edit(path, old, new)` | — (replaces `old` with `new`) |
| `bash(cmd, timeout=60)` | combined stdout/stderr |
| `search(pattern, path=".")` | matching lines |

`read` returns the file whole by default; `offset`/`limit` page it by line
(skip `offset` lines, return at most `limit`) instead of pulling a long file
into context. None of these cap their result — the content lands in a kernel
variable intact; the only cap is on what the agent chooses to `print()` back to
itself (see [the action space](../explanation/action-space.md#large-and-binary-payloads)).
`bash` runs with secret-bearing env vars stripped (see
[Security & audit](../explanation/security-and-audit.md)).

## Reaching the outside world is not a builtin

Everything that reaches an external system — the web, a browser, HTTP APIs, the
package index, MCP servers, learned skills — is a **tool**, not a builtin. None
are in scope by default; the agent discovers and loads them the same way
(`search_tools` → `describe_tool` → `use_tool`), and every call is gated exactly
as a builtin's is. The line: builtins are the agent's own body; tools are what it
reaches out to. The first-party external tools ship registered under the `web`
and `packages` categories — find them with `search_tools("web")` /
`search_tools("install")`:

| Tool | `search_tools` | What it is |
|------|----------------|------------|
| `web` | `web` | `search` (Anthropic server-side search) + `fetch` (one-shot GET returning a readable page map — HTML reduced to clean markdown content plus `## FORMS` (each form's action/method and every field) and `## LINKS` (navigable links, resolved to absolute URLs) so the agent can see what to click and fill; non-HTML verbatim; static HTML only, JS-rendered affordances need `browser`. A thin wrapper over `http.request`; `save="path"` or a binary body writes to the workspace and returns a note pointing at the file, still carrying the FORMS/LINKS map) |
| `http` | `web`, `http`, `api` | Stateful HTTP: `open_session` (cookies persist on the id across cells), `request` (returns `{status, url, headers, content_type, elapsed_ms, title, links, forms, text, path, bytes, preview, saved}` — `title`/`links`/`forms` are the parsed affordances, populated for HTML and riding inline even when the body spills to disk), `close_session`. POST/PUT bodies, multipart upload of a workspace file, named-secret injection |
| `browser` | `web`, `browser` | Headless Playwright lane: `open_browser` / `goto` / `click` / `fill` / `fill_secret` / `read_text` / `screenshot` / `close_browser`. Needs the `pyharness[browser]` extra + `playwright install chromium` |
| `packages` | `install` | `install` a PyPI package into the session venv for later `import` |

`describe_tool(name)` is the live source for each tool's exact signatures — the
docs don't duplicate them. The non-inferable semantics that survive the move:

- **Reads are free; state-changing calls need human approval.** GET/HEAD and page
  reads run unattended; POST/PUT/PATCH/DELETE and `click`/`fill`/`fill_secret`
  are gated per call. This holds whether the capability is a builtin or a tool.
- **Bodies come back whole — inline or on disk.** A textual response (and
  `browser.read_text`) rides back as `text`, uncapped, for the kernel to parse.
  A binary body, a response past the inline ceiling, or an explicit `save="path"`
  is written to the workspace instead: `text` is `None` and `path`/`bytes`/`preview`
  point at the file, which the agent reads with its own Python. Never a truncated
  head. See [the action space](../explanation/action-space.md#large-and-binary-payloads).
- **Secrets never round-trip through agent-visible text.** `auth`/`secret_fields`
  (http) and `fill_secret` (browser) name a vault secret resolved parent-side;
  the value is masked (`***`) out of every returned `url`/`text`/`headers` and
  out of `read_text`. `screenshot` writes to disk only, so a secret visible
  on-screen still appears in the image.
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

See [Use the secrets vault](../how-to/use-the-vault.md).

## Delegation

Fan bulk work out to cheaper models without filling the orchestrator's context.

| Signature | Notes |
|-----------|-------|
| `llm(prompt, tier="smart"\|"cheap", system=None) -> str` | one completion |
| `agent(task, tier=..., context=None) -> str` | a sub-agent that can itself act |
| `map_agents(tasks, tier="cheap", max_concurrency=8) -> list[Result]` | parallel sub-agents; each `Result` has `.ok`, `.value`, `.error` |

Tiers map to models in `pyharness/llm/client.py`: `smart` → Opus, `mid` →
Sonnet, `cheap` → Haiku. Use `cheap` for bulk/parallel work, `smart` for hard
reasoning. See [Budget](../explanation/budget.md).

## Tool discovery

Find a tool, inspect it, then load and call it.

| Signature | Returns |
|-----------|---------|
| `search_tools(query="", include_all=False) -> str` | ranked **headers** (name, summary, source/category); search by what you need (e.g. `"web"`), `include_all=True` or `"*"` lists the whole catalog |
| `describe_tool(name) -> str` | that tool's functions (signatures + docstrings); for a learned skill, also its instructions |
| `use_tool(name) -> module` | load it, then call its functions on the returned module |

## Skills

Package a repeatable procedure so this and later sessions can reuse it.

```python
save_skill(name, description, instructions, files=None, keywords=(), category=None) -> str
record_skill_use(name, outcome, note="") -> str
```

`instructions` is the markdown how-to; `files` is `{"helper.py": source, ...}` of
optional bundled modules. Persists to disk and registers as a learned tool. See
[Add a tool or save a skill](../how-to/add-a-tool-or-skill.md).

> Saving a skill requires human approval by default (it writes code that
> auto-loads in later sessions) — see [the approval policy](../explanation/security-and-audit.md).

**Trust is earned, not asserted.** A newly saved or revised skill is
**unverified** — it has never run successfully, so its steps are a hypothesis.
`record_skill_use(name, outcome, note="")` logs how a run went (`outcome` is
`"worked"` or `"failed"`); the first `"worked"` marks the skill `verified`. The
log is a bounded per-skill journal (`journal.json` beside `SKILL.md`) so a later
session sees how it last behaved. `search_tools` tags an `unverified` or
`last-failed` skill; `describe_tool` shows the verification state and recent uses
above the instructions. Recording a use writes only metadata, so it is *not*
gated for approval.

**Revising a skill** needs no separate builtin: `save_skill` with the same name
overwrites the prior version (stale bundled `.py` are dropped) and resets it to
unverified while keeping the use log, so the agent folds what it learned into the
instructions, re-saves, and re-earns trust on the next real run.

## Reflection

Read your own action record — the observe half of `do → observe → revise`.

| Signature | Returns |
|-----------|---------|
| `history(limit=20, action=None) -> list[dict]` | your recent actions, oldest last: `{ts, action, decision?, ok?, args?, error?}`. `action` filters by prefix (`"http"`, `"browser.click"`) |

The audit log lives at the session root, outside the workspace the file builtins
are confined to, so `history()` is the only way agent code reaches it. It records
*side effects* — every capability call, its decision (allow/approve/deny),
whether it succeeded, and a secret-safe summary of what was sent — so you can
confirm an effect landed (`request` returned 200), see why an action was refused,
and revise from what actually happened. Your own cells and notes are already in
context, so only the audit is surfaced. See
[Security & audit](../explanation/security-and-audit.md).
