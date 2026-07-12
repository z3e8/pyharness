# Builtins

The functions always in scope inside a `run_python` cell. Call them **directly by
bare name** — never import them. This list is the whole set; anything else is a
[tool](../how-to/add-a-tool-or-skill.md) you load on demand. Paths are relative
to the session workspace.

This is the authoritative contract the orchestrator is given (see
`SYSTEM_PROMPT` in `pyharness/core/agent.py`).

## Files & shell

| Signature | Returns |
|-----------|---------|
| `read(path)` | file contents |
| `write(path, content)` | — (creates/overwrites) |
| `edit(path, old, new)` | — (replaces `old` with `new`) |
| `bash(cmd, timeout=60)` | combined stdout/stderr |
| `search(pattern, path=".")` | matching lines |

`bash` runs with secret-bearing env vars stripped (see
[Security & audit](../explanation/security-and-audit.md)).

## Web

| Signature | Notes |
|-----------|-------|
| `web_search(query) -> str` | Anthropic server-side search; no extra API key |
| `web_fetch(url, auth=None, auth_style="bearer", auth_name=None, user=None) -> str` | `auth` names a secret, injected parent-side and never shown to the agent |
| `open_session() -> str` / `close_session(session_id)` | Open/close a persistent HTTP session; cookies persist on the id across cells |
| `request(session_id, method, url, *, ...) -> dict` | Stateful request on a session (or `None` for one-shot). Returns `{status, url, headers, text, truncated, elapsed_ms}` |

`web_fetch` auth styles: `"bearer"` · `"header"` (`auth_name` = header name) ·
`"query"` (`auth_name` = param) · `"basic"` (`user=...`).

`request` reuses those same auth styles (`auth` / `auth_style` / `auth_name` /
`auth_user`), plus `secret_fields={"field": "secret_name"}` to inject a named
secret into the `json`/`data` body and `files=[["field", "path"]]` to upload a
workspace file (read parent-side). Any secret injected this way is masked
(`***`) out of the returned `url`, `text`, and `headers`, so a `"query"`-style
auth secret echoed in the final url or a `secret_fields` value reflected in the
response cannot round-trip back to the agent. The live `httpx.Client` stays
parent-side, keyed by the session id; the agent only holds the id. State-changing
methods (POST/PUT/PATCH/DELETE) require human approval; reads do not. `web_fetch`
is a thin one-shot wrapper over `request`.

## Browser

Needs the optional `pyharness[browser]` extra plus `playwright install chromium`;
absent, the first call raises with that instruction.

| Signature | Notes |
|-----------|-------|
| `open_browser() -> str` / `close_browser(session_id)` | Launch/close a headless page; it and its cookies persist on the id across cells |
| `goto(session_id, url) -> dict` | Navigate. Returns `{url, title, status}` |
| `click(session_id, selector) -> dict` | Click a CSS/text selector. State-changing — approval |
| `fill(session_id, selector, value) -> dict` | Type non-secret text into a field — approval |
| `fill_secret(session_id, selector, secret_name) -> dict` | Type a named vault secret into a field — approval |
| `read_text(session_id, selector=None) -> dict` | Read visible text (whole page or one element). Returns `{text, truncated}` |
| `screenshot(session_id, path) -> dict` | Save a PNG to a workspace path. Returns `{path}` |

Like the HTTP session, the live Playwright page stays parent-side, keyed by the
id; the agent only holds the id. Secrets follow the same rule as `request`:
`fill_secret` names a vault secret, resolved parent-side and typed into the page,
never returned. Any secret injected this way is masked (`***`) out of every
`read_text` on that session, so a credential cannot round-trip back through
agent-visible text. `screenshot` writes to disk only — a secret visible on
screen appears in the image. State-changing actions (`click` / `fill` /
`fill_secret`) require human approval; navigation and reads do not.

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
| `search_tools(query="", include_all=False) -> str` | ranked **headers** (name, summary, source/category); empty query lists common tools, `include_all=True` surfaces the long tail |
| `describe_tool(name) -> str` | that tool's functions (signatures + docstrings); for a learned skill, also its instructions |
| `use_tool(name) -> module` | load it, then call its functions on the returned module |

## Skills

Package a repeatable procedure so this and later sessions can reuse it.

```python
save_skill(name, description, instructions, files=None, keywords=(), category=None) -> str
```

`instructions` is the markdown how-to; `files` is `{"helper.py": source, ...}` of
optional bundled modules. Persists to disk and registers as a learned tool. See
[Add a tool or save a skill](../how-to/add-a-tool-or-skill.md).

> Saving a skill requires human approval by default (it writes code that
> auto-loads in later sessions) — see [the approval policy](../explanation/security-and-audit.md).
