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

`web_fetch` auth styles: `"bearer"` · `"header"` (`auth_name` = header name) ·
`"query"` (`auth_name` = param) · `"basic"` (`user=...`).

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
