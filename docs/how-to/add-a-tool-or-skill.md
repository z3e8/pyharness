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

`search_tools("")` lists any *featured* tools; nothing is featured by default, so
search by what you need (e.g. `search_tools("web")`). `include_all=True` (or query
`"*"`) lists the whole catalog.

## Save a skill (a learned tool)

A **skill** packages a repeatable procedure — markdown instructions plus bundled
code — so it reloads in later sessions. The agent (or you) saves one:

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
`describe_tool` shows its instructions (the runbook) **and its bundled source**
(full text for small files, a def/class outline for large ones), `use_tool`
returns the bundled code as a module.

- **Put the code in `files`, not in the markdown.** Anything a later run would
  otherwise re-type — a fetch, a parse, a login sequence, a formatter — belongs
  in `files` as an importable function, so the next run calls
  `use_tool(name).render(...)` instead of rewriting it from the runbook. The
  instructions cover the procedure *around* the code.
- **Bundled code is lazy.** Function names, signatures, and docstrings are read
  from the source with `ast` — nothing in a bundled file executes at load,
  search, or describe time. The files run on the first actual function call
  (with the skill dir on `sys.path`, so they may import one another). A syntax
  error is reported by `describe_tool`; an import-time failure surfaces on the
  first call as an error naming the skill and file.
- **Bundled code has the session builtins in scope** — the same
  [builtins](../reference/builtins.md) agent cells get (`use_tool`, `read`,
  `llm`, …), seeded into each bundled module's globals when it first executes.
  Reach external capabilities the usual way (`web = use_tool("web")` then
  `web.fetch(url)`); never `import pyharness` — the builtins are not package
  exports, and that import fails with a pointer here. Every capability call a
  skill makes routes through the [broker](../explanation/broker.md) — policy,
  audit, budget, approvals — exactly as if the agent had made it in a cell, so
  a skill-mediated `web.fetch` and a direct one are indistinguishable in the
  audit chain.

- Skills live under `~/.pyharness/skills/<name>/` (override with
  `Session(skills_dir=...)`), one directory each: a `SKILL.md` (frontmatter +
  instructions), optional `*.py` modules, and a `journal.json` trust sidecar.
- **You can author one by hand** — just create that directory; it loads next
  session, no code required. A hand-authored skill also starts unverified.
- `save_skill` requires approval by default (it writes code that auto-runs
  later) — see [Security & audit](../explanation/security-and-audit.md).
- **Trust is earned by a real run.** A new or revised skill is `unverified`
  (tagged in `search_tools`, spelled out in `describe_tool`) — treat its steps as
  a hypothesis. After running it, call `record_skill_use(name, "worked"|"failed",
  note=...)`; the first `"worked"` marks it `verified`, and the bounded journal
  lets a later session see how it last behaved and catch a breaking change.
- **Revising a skill: prefer `edit_skill(name, edits, reason="")`.** `edits` is
  a list of `{"old": ..., "new": ...}` deltas — a surgical fix that keeps every
  detail not being corrected, rather than regenerating the whole runbook.
  Frontmatter and bundled files are untouched; the revision resets to
  unverified while keeping the use log. `save_skill` with the same name still
  fully replaces a skill (stale bundled `.py` are pruned) when a rewrite is
  genuinely intended. After a run, the agent reads `history()` and the skill's
  journal to see what happened and folds the lesson in via `edit_skill` — that
  is the do → observe → revise loop.

## Mount an MCP server

Three ways in, same result — one lazily-connected tool module per server:

1. **Config file.** Declare servers in `.mcp.json` at the repo root (or point
   `PYHARNESS_MCP_CONFIG` elsewhere); the CLI mounts them when the file exists:

   ```json
   {
     "mcpServers": {
       "weather": {
         "command": "pnpm", "args": ["dlx", "@aiagentkarl/weather-mcp-server"],
         "summary": "Weather lookups.", "keywords": ["forecast"], "category": "data"
       }
     }
   }
   ```

   The repo ships this exact config as `.mcp.json.example` (copy it to
   `.mcp.json` to activate). `.mcp.json` itself is **gitignored** so a fresh
   checkout never auto-mounts a server on startup: the `weather` entry above is
   an unverified third-party npm package run via `pnpm dlx` — an illustrative
   example, not a vetted dependency. Review any server you mount.

   Local servers use `command`/`args`; remote ones use `url` (+ `headers`).
   Optional `summary`/`keywords`/`category`/`featured` feed `search_tools`
   ranking — worth setting, since a lazy server is otherwise findable only by
   its name. `timeout` (seconds) bounds each request to that server.
2. **`pyharness-mcp add/list/rm`** edits the same file from the shell — see
   [CLI](../reference/cli.md).
3. **`add_mcp_server(...)` in-session** (approval-gated): the agent mounts a
   server mid-session; `save=True` persists it to the config file for later
   sessions. See [Builtins](../reference/builtins.md).

Each server's tools are wrapped as one tool module named after the server,
connected **lazily** on first `describe`/`use` — so a slow or down server never
delays startup or browsing. In code, pass `Session(mcp_config=...)` (a path or
a dict; a path is remembered even before the file exists so `save=True` can
create it).

Credential values in `env`/`headers` should be vault refs (`"secret:NAME"`) —
resolved parent-side, never visible to the agent, and the only form the save
paths will write to disk. Calls on MCP tools are broker-gated per the server's
declared annotations (read-only flows; anything else prompts, grantable per
server; destructive always re-asks) — see
[Security & audit](../explanation/security-and-audit.md).

## Install a package

The agent can `pip install` into a per-session venv via the `packages` capability
(requires out-of-process mode, and approval by default). Prefer a skill's bundled
files or an MCP server when the dependency is known ahead of time.
