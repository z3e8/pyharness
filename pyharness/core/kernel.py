from __future__ import annotations

import io
import traceback
from contextlib import redirect_stderr, redirect_stdout

from ..util import truncate


class Kernel:
    """The session's persistent Python namespace — like a Jupyter kernel.

    Each `run` is a cell. Variables, imports, and functions persist across cells.
    Only captured stdout/stderr (what the agent prints) is returned; everything
    else stays in the namespace, unseen by the orchestrator. State is in-memory
    and disposable (durable resume is a later version)."""

    def __init__(self, namespace: dict[str, object]):
        self.namespace = dict(namespace)

    def run(self, code: str) -> str:
        out = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(out):
                exec(compile(code, "<cell>", "exec"), self.namespace)
        except Exception:
            out.write(traceback.format_exc(limit=5))
        return truncate(out.getvalue().rstrip()) or "(no output)"
