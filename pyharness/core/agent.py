from __future__ import annotations

import time
from typing import Callable

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

Functions available in the kernel (paths are relative to the session workspace):
  Files & shell:
    read(path) / write(path, content) / edit(path, old, new)
    bash(cmd, timeout=60)
    search(pattern, path=".")
  Web:
    web_search(query) -> str
    web_fetch(url) -> str
  Delegation — do bulk work without filling your own context:
    llm(prompt, tier="smart"|"cheap", system=None) -> str
    agent(task, tier=..., context=None) -> str
    map_agents(tasks, tier="cheap", max_concurrency=8) -> list[Result]
        each Result has .ok, .value, .error
  Tools:
    search_tools(query) -> str   # discover installed tools (returns their interface)
    use_tool(name) -> module     # import a tool, then call its functions

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

        for _ in range(self.max_steps):
            self.budget.check()

            t0 = time.time()
            cost_before = self.budget.spent_usd
            prompt_snapshot = _serialize_messages(messages)

            completion = self.llm.complete(
                system=SYSTEM_PROMPT,
                messages=messages,
                tier=self.tier,
                tools=[RUN_PYTHON_TOOL],
            )

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
