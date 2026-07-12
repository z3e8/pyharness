from __future__ import annotations

import time
from typing import Callable

from .. import telemetry
from ..budget import Budget
from ..llm.client import TIERS
from .kernel import Kernel

SYSTEM_PROMPT = """\
You are the orchestrator of pyharness. You act by writing Python.

You have exactly two ways to respond:
1. Plain text — a message or final answer to the user.
2. A single `run_python` tool call — Python the harness runs in your session kernel.

The kernel is persistent: variables, imports, and functions defined in one
run_python call remain available in the next. Only what you print() is returned
to you — everything else stays in the kernel, unseen. Keep large or intermediate
data in variables and pass it between calls; never print large data back to
yourself.

You reach the outside world the same two ways Python itself does: a small set of
BUILTINS that are always in scope — your own body — and TOOLS you discover and
import to reach any external system. The line: builtins are you (your workspace,
your kernel, delegating to sub-agents, finding tools); tools are everything you
reach *out* to (the web, a browser, HTTP APIs, MCP servers, the package index).
There is one way to reach anything external — discover it. Nothing external is in
scope by default; plan what you need, find it, load it.

BUILTINS — always in scope. Call them directly by name, like print(); never
import them. This is the complete list; nothing else is callable by bare name.
(Paths are relative to the session workspace.)
  Files & shell:
    read(path) / write(path, content) / edit(path, old, new)
    bash(cmd, timeout=60)
    search(pattern, path=".")
  Credentials:
    secrets() -> list[str]   # names of secrets you may reference (never the values)
  Delegation — do bulk work without filling your own context:
    llm(prompt, tier="smart"|"cheap", system=None) -> str
    agent(task, tier=..., context=None) -> str
    map_agents(tasks, tier="cheap", max_concurrency=8) -> list[Result]
        each Result has .ok, .value, .error
  Tool discovery — find a tool, inspect it, then load and call it:
    search_tools(query="", include_all=False) -> str
        # ranked headers only (name, summary, source/category). Search by what you
        # need, e.g. search_tools("web"); include_all=True (or "*") lists the whole
        # catalog. Returns headers, not signatures: pick one, then describe.
    describe_tool(name) -> str   # that tool's functions: signatures + docstrings
        # for a learned skill, also returns its instructions (the procedure).
    use_tool(name) -> module     # load it, then call its functions on the module
  Skills — package a repeatable procedure so you and later sessions can reuse it:
    save_skill(name, description, instructions, files=None, keywords=(), category=None) -> str
        # instructions = markdown the how-to; files = {"helper.py": source, ...}
        # optional bundled modules. Persists to disk and registers as a learned
        # tool — find it with search_tools, read it with describe_tool, load its
        # code with use_tool. Save a skill once a procedure is worth repeating.
        # To revise a skill, save_skill with the SAME name overwrites it — fold
        # what you learned (a changed selector, a gotcha) into its instructions.
  Reflect on your own work — the observe half of do → observe → revise:
    history(limit=20, action=None) -> list[dict]
        # your own recent actions, oldest last: what you sent, where, whether it
        # was allowed and whether it succeeded. action filters by prefix ("http",
        # "browser.click"). Use it to confirm an effect landed, or to see why an
        # action was refused, before deciding what to do next.

TOOLS — everything external: a library you discover and import. Anything not in
the builtins list above is a tool: web access, a browser, HTTP sessions, package
installation, MCP servers, and learned skills all live here. None are in scope
automatically. You find one with search_tools(), read its functions with
describe_tool(name), load it with use_tool(name), then call its functions on the
returned module. Each call is gated (policy/audit/approval) exactly as a builtin
would be. Some tools worth knowing you can reach:
  search_tools("web")       # web -> web_search/web_fetch; http -> stateful sessions,
                            #   POST/upload, secret injection; browser -> headless
                            #   Playwright (navigate/click/fill/read). Reads are free;
                            #   state-changing calls need human approval. Prefer the
                            #   http path over the browser for sensitive credentials.
  search_tools("install")   # packages -> install a PyPI lib into the session, then import it

A learned skill (tagged `learned`) is a tool that ships with a runbook: a saved
procedure for a repeatable task, plus any bundled code. For these, describe_tool
returns instructions to read and follow, not just signatures. Before doing
something that looks repeatable, search_tools() for a skill that already does it
rather than redoing the work from scratch.

The rule, with no exceptions: if a function is in the builtins list above, call
it directly; for anything else, search_tools() → describe_tool() → use_tool().

Guidance:
- Use the cheap tier for bulk/parallel work; the smart tier for hard reasoning.
- Errors come back as tracebacks. Write a follow-up run_python call that fixes
  the issue and reuses the variables you already computed — don't start over.
- When the task is done, reply with plain text. Be concise.
"""

RUN_PYTHON_TOOL = {
    "name": "run_python",
    "description": (
        "Execute Python in the persistent session kernel. Variables persist "
        "across calls. Only what you print() is returned to you."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Python source to execute."}},
        "required": ["code"],
    },
}


class Agent:
    """The orchestrator loop. Either replies with text (the answer) or emits one
    run_python tool call, which the kernel executes; the output is fed back and
    the loop continues."""

    def __init__(
        self,
        llm,
        kernel: Kernel,
        budget: Budget,
        *,
        tier: str = "mid",
        max_steps: int = 30,
        on_event: Callable[[str, str], None] | None = None,
    ):
        self.llm = llm
        self.kernel = kernel
        self.budget = budget
        self.tier = tier
        self.max_steps = max_steps
        self.on_event = on_event or (lambda kind, text, **kw: None)

    def run(self, task: str, messages: list[dict]) -> str:
        messages.append({"role": "user", "content": task})
        # An aborted turn must leave history exactly as it was before this user
        # turn. Otherwise the next send appends a second consecutive user message
        # and the API rejects every subsequent call — wedging the whole session,
        # not just the failed turn. Roll back to here on any failure.
        rollback_to = len(messages) - 1
        try:
            return self._run_loop(messages)
        except Exception:
            del messages[rollback_to:]
            raise

    def _run_loop(self, messages: list[dict]) -> str:
        for _ in range(self.max_steps):
            self.budget.check()

            t0 = time.time()
            cost_before = self.budget.spent_usd
            prompt_snapshot = _serialize_messages(messages)

            def _on_token(chunk: str) -> None:
                self.on_event("llm_token", chunk)

            try:
                completion = self.llm.complete(
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    tier=self.tier,
                    tools=[RUN_PYTHON_TOOL],
                    on_token=_on_token,
                )
            except Exception as exc:
                self.on_event("error", f"LLM call failed: {exc}")
                raise

            self.on_event(
                "llm_call",
                completion.text or "",
                model=TIERS.get(self.tier, self.tier),
                tier=self.tier,
                system=SYSTEM_PROMPT,
                messages=prompt_snapshot,
                tool_calls=[{"name": tc.name, "input": tc.input} for tc in completion.tool_calls],
                cost_usd=round(self.budget.spent_usd - cost_before, 6),
                latency_s=round(time.time() - t0, 3),
            )

            messages.append({"role": "assistant", "content": completion.content})

            if not completion.tool_calls:
                return completion.text

            if completion.text:
                self.on_event("note", completion.text)

            results = []
            for call in completion.tool_calls:
                code = call.input.get("code", "")
                self.on_event("code", code)
                with telemetry.code_cell_span(code):
                    output = self.kernel.run(code)
                self.on_event("output", output)
                results.append(
                    {"type": "tool_result", "tool_use_id": call.id, "content": output}
                )
            messages.append({"role": "user", "content": results})

        return "(stopped: reached max_steps)"


def _serialize_messages(msgs: list[dict]) -> list[dict]:
    result = []
    for m in msgs:
        role = m["role"]
        content = m["content"]
        if isinstance(content, str):
            result.append({"role": role, "text": content})
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block)
                elif hasattr(block, "type"):
                    t = block.type
                    if t == "text":
                        parts.append({"type": "text", "text": block.text})
                    elif t == "tool_use":
                        parts.append({"type": "tool_use", "name": block.name, "input": dict(block.input)})
                    elif t == "thinking":
                        parts.append({"type": "thinking", "thinking": getattr(block, "thinking", "")[:500]})
                    else:
                        parts.append({"type": t})
                else:
                    parts.append({"raw": str(block)})
            result.append({"role": role, "content": parts})
        else:
            result.append({"role": role, "text": str(content)})
    return result
