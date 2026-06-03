from __future__ import annotations

import io
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

from .tools import truncate


@dataclass
class Execution:
    output: str  # captured stdout + stderr
    error: str | None  # traceback if the code raised

    def feedback(self) -> str:
        parts = []
        if self.output.strip():
            parts.append(self.output.rstrip())
        if self.error:
            parts.append(self.error.rstrip())
        return truncate("\n".join(parts)) or "(no output)"


class Executor:
    """Runs agent code in a persistent namespace, capturing all output."""

    def __init__(self, namespace: dict):
        self._ns = namespace

    def run(self, code: str) -> Execution:
        out = io.StringIO()
        error: str | None = None
        try:
            with redirect_stdout(out), redirect_stderr(out):
                exec(compile(code, "<agent>", "exec"), self._ns)
        except Exception:
            error = traceback.format_exc(limit=5)
        return Execution(output=out.getvalue(), error=error)
