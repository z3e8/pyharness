# Skills

A **skill** is a learned tool the agent (or a human) saves once and reuses across
sessions. It is the harness's answer to "remember how to do this thing" — design
[§6](../agents/design.md) makes skills and tools the same kind of object (callable
modules in one registry, `source="learned"`); this is the layer that lets a skill
also carry *instructions*, persist on disk, and be authored at runtime.

## What a skill is

A skill is a directory under the skills root:

```
<skills_root>/<name>/
    SKILL.md        # frontmatter (name, description, keywords, category)
                    # + body = the procedure / instructions
    *.py            # optional bundled modules, imported on first use
```

`SKILL.md`:

```markdown
---
name: dedupe-csv
description: Drop duplicate rows from a CSV by a key column
keywords: csv, dedupe, dataframe
category: data
---

1. Read the CSV with `use_tool("dedupe-csv")` then call `run(path, key)`.
2. It keeps the first occurrence of each key and rewrites the file in place.
3. Watch out for files with no header row — pass `header=False`.
```

The two halves map onto the two things a large skill needs:

- **Markdown instructions** — the *how-to*. Procedural knowledge the agent reads:
  steps, gotchas, when to use it. A skill may be instructions only.
- **Bundled `.py` code** — the *callable part*. Public functions from every
  bundled file are exposed as one module named after the skill. Code may be
  omitted; instructions may reference builtins or other tools instead.

## How it surfaces to the agent

Skills are tools, so they ride the existing two-level discovery
([tool-discovery.md](tool-discovery.md)) with one addition:

| call | a normal tool | a skill |
| --- | --- | --- |
| `search_tools()` | ranked header | ranked header, tagged `learned` — found by query, **not** shown in the default browse |
| `describe_tool(name)` | function signatures + docstrings | **the instructions body**, then any bundled function signatures |
| `use_tool(name)` | the live module | the module built from the bundled `.py` |

Skills are **search-only, not featured**: an empty `search_tools()` lists the
common tools you reach for on every task, and a skill saved for one workflow
doesn't belong there. Skills surface when the agent queries a matching word
(name, description, or keyword) — so the discoverability lever is the agent's
*habit* of searching for a skill before redoing repeatable work, which the system
prompt nudges, not a default listing.

Discovery never imports a skill's code: headers come from the `SKILL.md`
frontmatter, and the instructions render from text. Bundled `.py` is imported only
on `describe`/`use` (lazily, via `Registry.register_lazy`'s path), so a skill with
broken code still shows its instructions and a "bundled code unavailable" note.

## Authoring

**Humans** write a skill directory by hand under the skills root and it loads next
session. **The agent** authors one at runtime with the `save_skill` builtin:

```python
save_skill(
    name="dedupe-csv",
    description="Drop duplicate rows from a CSV by a key column",
    instructions="Call run(path, key). Keeps the first occurrence...",
    files={"impl.py": "def run(path, key):\n    ...\n"},
    keywords=("csv", "dedupe"),
    category="data",
)
```

`save_skill` writes the directory **parent-side** (so it works out-of-process,
where the child can't touch disk), registers the skill immediately for the current
session, and — because it lives on disk — reloads automatically in later ones.
Re-saving a skill replaces it exactly: bundled `.py` from the prior version is
dropped first, so a renamed or removed helper can't linger and be imported.

Because a skill is agent-authored code that auto-loads in later sessions,
`save_skill` requires approval by default (`require_approval={"skills.save_skill"}`
on the session policy) — a human signs off at author time. Pass your own `Policy`
to change this.

## Where skills live

Skills are cross-session by design, so the root is **not** the per-session
workspace. It defaults to `~/.pyharness/skills` (consistent with the vault's
`~/.pyharness/`), overridable per session with `Session(skills_dir=...)` — point it
at a project directory to scope a skill set to one project.

## Trust

A skill's bundled code is imported into the kernel when `use_tool` loads it — the
same trust posture as any learned tool (design §6): in-process it runs in the host
namespace; out-of-process its functions route back through the broker via
`tools.invoke`, gaining the same policy / audit / budget gating as every other
capability. Agent-authored skills are LLM-written code that auto-loads in later
sessions, so the skills root is a trust boundary — treat it like code you'd run,
and scope it with `skills_dir` when running untrusted tasks. As a first line of
defence, `save_skill` requires approval by default (see Authoring), so a human
sees the code before it is ever persisted.
