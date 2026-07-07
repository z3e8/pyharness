# Add a tool or save a skill

*Goal: give the agent a new capability it can discover and load.*

Builtins are always in scope; everything else is a **tool** the agent finds with
`search_tools()` and loads with `use_tool()`. A **skill** is a tool a human or
the agent saves once and reuses across sessions.

```python
save_skill(name, description, instructions, files=[...])
```

<!-- TODO: where skills are stored (~/.pyharness/skills/<name>/, overridable via
Session(skills_dir=...)); describe_tool vs use_tool; how the registry
(pyharness/tools/registry.py) discovers tools; MCP tools. -->
