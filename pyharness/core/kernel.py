from __future__ import annotations

import traceback
from contextlib import redirect_stderr, redirect_stdout

from ..util import CaptureBuffer, truncate


class Kernel:
    """The session's persistent Python namespace — like a Jupyter kernel.

    Each `run` is a cell. Variables, imports, and functions persist across cells.
    Only captured stdout/stderr (what the agent prints) is returned; everything
    else stays in the namespace, unseen by the orchestrator. State is in-memory
    and disposable (durable resume is a later version).

    UNSAFE — test-only. Cells exec() in the HOST process: agent code can reach
    the host's os.environ (API keys, vault passphrase) and the live vault/broker
    by introspection. Sessions select it only via `unsafe_in_process=True`; the
    default is the out-of-process `RemoteKernel` (broker/remote)."""

    def __init__(self, namespace: dict[str, object]):
        self.namespace = dict(namespace)

    def run(self, code: str) -> str:
        out = CaptureBuffer()  # bound memory: a runaway print can't OOM the host
        try:
            with redirect_stdout(out), redirect_stderr(out):
                exec(compile(code, "<cell>", "exec"), self.namespace)
        except Exception:
            out.write(traceback.format_exc(limit=5))
        return truncate(out.getvalue().rstrip()) or "(no output)"
