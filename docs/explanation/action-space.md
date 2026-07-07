# The `run_python` action space

*Understanding-oriented: why the agent writes Python instead of calling JSON
tools.*

The orchestrator does exactly two things: reply with text, or emit one
`run_python` call. A session is a persistent kernel; each `run_python` is a cell
and variables persist across cells. Only what the agent `print()`s returns to its
context — large data stays in variables, unseen.

<!-- TODO: the argument for this design over fine-grained tool calls — token
economy, composition, the "reach the world the way Python does" model. Contrast
builtins-always-in-scope vs tools-on-demand. -->
