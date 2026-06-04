from __future__ import annotations

import io
import re
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .llm import LLM, Message
from .workspace import Workspace, truncate

SYSTEM_PROMPT = """\
You are an autonomous agent with exactly two valid outputs.

1. Plain text, for normal responses and final answers. Do not include fenced code.
2. One Python code block, when you need the harness to act:

```python
# your code here
```

The harness runs that code in a persistent namespace and sends back captured
stdout, stderr, or traceback. Use print() to inspect results.

Do not mix text and code in the same reply.

Available functions:
  bash(cmd, timeout=60) -> str
  read(path) -> str
  write(path, content) -> str
  edit(path, old, new) -> str
  search(pattern, path=".") -> str

Relative paths are inside the workspace.
"""

_CODE_BLOCK = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.DOTALL)
_FENCE = "```"
_RETRY_MESSAGE = (
    "Invalid output. Reply with exactly one of: plain text with no fenced code "
    "blocks, or a single fenced python code block and nothing else."
)


class OutputKind(str, Enum):
    TEXT = "text"
    PYTHON = "python"


@dataclass(frozen=True)
class AgentOutput:
    kind: OutputKind
    content: str


def parse_output(reply: str) -> AgentOutput | None:
    """Parse a model reply into the only two valid agent outputs."""
    if _FENCE not in reply:
        return AgentOutput(OutputKind.TEXT, reply)

    matches = list(_CODE_BLOCK.finditer(reply))
    if len(matches) != 1:
        return None

    match = matches[0]
    if reply[: match.start()].strip() or reply[match.end() :].strip():
        return None

    return AgentOutput(OutputKind.PYTHON, match.group(1))


class _PythonRunner:
    def __init__(self, namespace: dict[str, object]):
        self.namespace = namespace

    def run(self, code: str) -> str:
        output = io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(output):
                exec(compile(code, "<agent>", "exec"), self.namespace)
        except Exception:
            output.write(traceback.format_exc(limit=5))

        return truncate(output.getvalue().rstrip()) or "(no output)"


class Agent:
    def __init__(
        self,
        llm: LLM,
        workspace: Workspace | str | Path,
        *,
        max_steps: int = 20,
    ):
        self._llm = llm
        self._max_steps = max_steps
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        self.namespace: dict[str, object] = {
            "bash": self.workspace.bash,
            "read": self.workspace.read,
            "write": self.workspace.write,
            "edit": self.workspace.edit,
            "search": self.workspace.search,
        }
        self._runner = _PythonRunner(self.namespace)

    def run(self, task: str) -> str:
        messages = [Message("user", task)]
        for _ in range(self._max_steps):
            reply = self._llm.complete(SYSTEM_PROMPT, messages)
            messages.append(Message("assistant", reply))
            output = parse_output(reply)
            if output is None:
                messages.append(Message("user", _RETRY_MESSAGE))
                continue
            if output.kind is OutputKind.TEXT:
                return output.content
            messages.append(Message("user", self._runner.run(output.content)))
        return "(stopped: reached max_steps)"
