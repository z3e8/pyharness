# Add a tool or save a skill

*Goal: give the agent a capability beyond the builtins.* Everything that isn't a
[builtin](../reference/builtins.md) is a **tool** — a Python module of functions.
Built-in tools, locally installed modules, MCP servers, and learned skills all
live in one [registry](../explanation/broker.md) and are found the same way.

## How the agent finds and uses a tool

Discovery is two-level so a big catalog never floods the agent's context:

```python
search_tools("weather")     # ranked headers only — name, summary, source/category
describe_tool("weather")    # the chosen tool's functions: signatures + docstrings
weather = use_tool("weather")   # load it, then call its functions
weather.forecast("Boston")
```

`search_tools("")` lists the common (featured) tools; `include_all=True` (or
query `"*"`) surfaces the long tail.

## Save a skill (a learned tool)

A **skill** packages a repeatable procedure — markdown instructions plus optional
bundled code — so it reloads in later sessions. The agent (or you) saves one:

```python
save_skill(
    "release_notes",
    "Draft release notes from a git range",
    instructions="1. `git log`… 2. group by type… 3. render markdown.",
    files={"render.py": "def render(commits): ..."},
    keywords=("changelog", "notes"),
)
```

It's stored on disk and registered as a `learned` tool: `search_tools` finds it,
`describe_tool` shows its instructions (the runbook), `use_tool` loads the bundled
code.

- Skills live under `~/.pyharness/skills/<name>/` (override with
  `Session(skills_dir=...)`), one directory each: a `SKILL.md` (frontmatter +
  instructions) and optional `*.py` modules.
- **You can author one by hand** — just create that directory; it loads next
  session, no code required.
- `save_skill` requires approval by default (it writes code that auto-runs
  later) — see [Security & audit](../explanation/security-and-audit.md).

## Mount an MCP server

Declare servers in `.mcp.json` at the repo root (or point `PYHARNESS_MCP_CONFIG`
elsewhere); the CLI mounts them when the file exists:

```json
{
  "mcpServers": {
    "weather": { "command": "pnpm", "args": ["dlx", "@aiagentkarl/weather-mcp-server"] }
  }
}
```

Local servers use `command`/`args`; remote ones use `url`. Each server's tools are
wrapped as one tool module named after the server, connected **lazily** on first
`describe`/`use` — so a slow or down server never delays startup or browsing.
In code, pass `Session(mcp_config=...)` (a path or a dict).

## Install a package

The agent can `pip install` into a per-session venv via the `packages` capability
(requires out-of-process mode, and approval by default). Prefer a skill's bundled
files or an MCP server when the dependency is known ahead of time.
