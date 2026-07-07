# Builtins

*Precise reference: the functions always in scope inside `run_python`.* Relative
paths resolve inside the session workspace.

| Builtin | Purpose |
|---------|---------|
| `read` `write` `edit` | files |
| `bash` | shell |
| `search` | code/text search |
| `web_search` `web_fetch` | web |
| `llm` `agent` `map_agents` | delegation to models / sub-agents |
| `search_tools` `use_tool` `describe_tool` | tool discovery + loading |
| `save_skill` | persist a reusable skill |

<!-- TODO: exact signatures, return values, and what returns to context vs stays
in a variable, per builtin. -->
