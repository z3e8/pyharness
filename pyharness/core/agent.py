from __future__ import annotations

import platform
import time
from datetime import datetime
from pathlib import Path
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
yourself. A fetched page, API response, file read, or command output arrives in
your Python whole, never a truncated head — hold it in a variable, then search,
slice, and print only the part you need. A binary or very large body instead
lands in your workspace as a file: the call returns its path (with a short
preview), and you read or parse it from disk.

Your Python has no ambient powers. It cannot reach the network and holds no
credentials: a bare socket, `urllib`, or `requests` call, or reading a key from
`os.environ`, fails or comes back empty — by design, not by accident. That
failure (a connection error, an empty value) means "route it through a tool," not
"retry." Everything is reached two ways: BUILTINS, always in scope — your own
body (workspace, shell, kernel, delegation, finding tools); and TOOLS you
discover and load to reach anything external (the web, a browser, HTTP APIs, MCP
servers, the package index). Nothing external is in scope by default: plan what
you need, find it, load it.

BUILTINS — always in scope. Call them directly by name, like print(); never
import them. This is the complete list; nothing else is callable by bare name.
(Paths are relative to the session workspace.)
  Files & shell:
    read(path, offset=0, limit=None) / write(path, content) / edit(path, old, new)
        # read returns the file whole; offset/limit page a long one by line.
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
    record_skill_use(name, outcome, note="") -> str
        # after actually running a skill, log how it went: outcome "worked" or
        # "failed", plus a short note (a changed selector, why it broke). The
        # first "worked" marks the skill verified; the log lets you and later
        # sessions see how it last behaved and catch a breaking change.
  Reflect on your own work — the observe half of do → observe → revise:
    history(limit=20, action=None) -> list[dict]
        # your own recent actions, oldest last: what you sent, where, whether it
        # was allowed and whether it succeeded. action filters by prefix ("http",
        # "browser.click"). Use it to confirm an effect landed, or to see why an
        # action was refused, before deciding what to do next.

TOOLS — everything external. Anything not in the builtins list above is a tool:
web access, a browser, HTTP sessions, package installation, MCP servers, and
learned skills. None are in scope automatically. Find one with search_tools(),
read its functions with describe_tool(name), load it with use_tool(name), then
call its functions on the returned module. Each call is gated
(policy/audit/approval) exactly as a builtin would be. Some worth knowing:
  search_tools("web")       # web -> search/fetch; http -> stateful sessions,
                            #   POST/upload, secret injection; browser -> headless
                            #   Playwright (navigate/snapshot the page for element
                            #   refs/click/fill by ref/look — a screenshot you
                            #   see/read). Reads are free;
                            #   state-changing calls need human approval. Prefer the
                            #   http path over the browser for sensitive credentials.
  search_tools("install")   # packages -> install a PyPI lib into the session, then import it

A learned skill (tagged `learned`) is a tool that ships with a runbook —
describe_tool returns instructions to read and follow, not just signatures.
Before doing something repeatable, search_tools() for a skill that already does
it. But a skill is agent-authored, so it may be wrong: `unverified` means it has
never run successfully — treat its steps (endpoints, selectors, auth) as a
hypothesis, confirm them before relying on it; `last-failed` means its last run
broke — read the log in describe_tool, fix the instructions, re-save. Prefer a
skill that has worked. After running one, record_skill_use() so trust reflects
reality — a success earns `verified`, a failure warns the next session.

The rule, with no exceptions: if a function is in the builtins list above, call
it directly; for anything else, search_tools() → describe_tool() → use_tool().

Guidance:
- Use the cheap tier for bulk/parallel work; the smart tier for hard reasoning.
- Errors come back as tracebacks. Write a follow-up run_python call that fixes
  the issue and reuses the variables you already computed — don't start over.
- You run under a bounded step count and a spend budget. Be economical; for long
  work, checkpoint state to the workspace as you go so it survives a stop.
- Fail fast and honestly. When a surface structurally resists — the same call
  fails twice the same way, an element the page shows can't be interacted with, a
  login is behind a CAPTCHA/2FA/checkpoint, an API returns 401/403 — that is a
  wall, not a tweak-the-input problem. Stop, state plainly what you observed and
  why it blocks the task, and hand the decision back. Do not grind through
  selector or parameter variations hoping one sticks; a wrong answer dressed up
  as success is worse than a clear "this is blocked, here's why."
- When the task is done, reply with plain text. Be concise.
"""


def render_context(workspace_root: str | Path | None, *, now: datetime | None = None) -> str:
    """The dynamic session preamble appended to SYSTEM_PROMPT each turn. Static
    prose carries the rules; this carries the world-state the model would
    otherwise burn turns discovering — the date (its training cutoff silently
    substitutes otherwise), the platform, and where its relative paths resolve.
    Deliberately minimal; grow it only with facts the agent cannot cheaply see."""
    now = now or datetime.now().astimezone()
    lines = [
        "## Session",
        f"- Now: {now:%Y-%m-%d %H:%M %Z} ({now:%A})",
        f"- Platform: {platform.system().lower()} / {platform.machine()}",
    ]
    if workspace_root is not None:
        lines.append(f"- Workspace: {workspace_root} — your relative paths resolve here")
    return "\n".join(lines)


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
        workspace_root: str | Path | None = None,
        on_event: Callable[[str, str], None] | None = None,
        media=None,
    ):
        self.llm = llm
        self.kernel = kernel
        self.budget = budget
        self.tier = tier
        self.max_steps = max_steps
        self.workspace_root = workspace_root
        self.on_event = on_event or (lambda kind, text, **kw: None)
        # Parent-side outbox a cell's capabilities fill with images (browser.look);
        # drained after each kernel.run into the tool_result's content blocks.
        self.media = media

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
        system = f"{SYSTEM_PROMPT}\n\n{render_context(self.workspace_root)}"
        for _ in range(self.max_steps):
            self.budget.check()

            t0 = time.time()
            cost_before = self.budget.spent_usd
            prompt_snapshot = _serialize_messages(messages)

            def _on_token(chunk: str) -> None:
                self.on_event("llm_token", chunk)

            try:
                completion = self.llm.complete(
                    system=system,
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
                system=system,
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
                # A cell may have staged images (browser.look). With none, the
                # tool_result content stays a plain string — unchanged for every
                # text-only cell; with images it becomes a text block + image blocks.
                images = self.media.drain() if self.media is not None else []
                content = output if not images else [{"type": "text", "text": output}, *images]
                results.append(
                    {"type": "tool_result", "tool_use_id": call.id, "content": content}
                )
            messages.append({"role": "user", "content": results})

        return "(stopped: reached max_steps)"


def _elide_image_data(obj):
    """Replace base64 image payloads with a small summary, recursing through the
    lists and dicts a tool_result nests. Trace snapshots and the llm_call event go
    through here so a screenshot doesn't bloat trace.jsonl; the real message
    history keeps the full block."""
    if isinstance(obj, dict):
        if obj.get("type") == "image" and isinstance(obj.get("source"), dict):
            source = obj["source"]
            return {"type": "image", "media_type": source.get("media_type"), "bytes": len(source.get("data", ""))}
        return {key: _elide_image_data(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_elide_image_data(item) for item in obj]
    return obj


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
                    parts.append(_elide_image_data(block))
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
