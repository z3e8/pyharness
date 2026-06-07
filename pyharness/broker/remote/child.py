from __future__ import annotations

import io
import traceback
from contextlib import redirect_stderr, redirect_stdout

from ...util import truncate
from .protocol import RemoteToolSpec
from .sandbox import apply_resource_limits


def child_main(conn, op_names: list[str]) -> None:
    """Entry point for the child process (spawned by the host).

    The child is the agent's userland: it holds the persistent namespace and a
    proxy for each capability, but no broker, vault, LLM client, or capability
    code. Every side effect is a proxy call that crosses the pipe to the parent;
    pure computation (loops, data wrangling, the agent's own variables) stays
    here, unseen by the orchestrator."""
    apply_resource_limits()
    namespace = {op: _make_proxy(conn, op) for op in op_names}
    while True:
        msg = conn.recv()
        if msg[0] == "shutdown":
            return
        if msg[0] == "run":
            conn.send(("done", _run_cell(conn, namespace, msg[1])))


def _run_cell(conn, namespace: dict, code: str) -> str:
    """Exec one cell against the persistent namespace, mirroring the in-process
    Kernel: only captured stdout/stderr (and tracebacks) return to the parent;
    every other value stays live in the namespace for later cells."""
    out = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(out):
            exec(compile(code, "<cell>", "exec"), namespace)
    except Exception:
        out.write(traceback.format_exc(limit=5))
    return truncate(out.getvalue().rstrip()) or "(no output)"


def _make_proxy(conn, op: str):
    def proxy(*args, **kwargs):
        conn.send(("call", op, args, kwargs))
        return _recv_result(conn)

    proxy.__name__ = op
    return proxy


def _recv_result(conn):
    tag, payload = conn.recv()
    if tag == "err":
        raise payload
    if isinstance(payload, RemoteToolSpec):
        return _RemoteModule(conn, payload)
    return payload


class _RemoteModule:
    """A proxy for a tool module. Attribute access yields callables that route
    each invocation back to the parent's broker (`tools.invoke`), so a tool's
    functions are policy/audit/budget-gated exactly like any other capability."""

    def __init__(self, conn, spec: RemoteToolSpec):
        self._conn = conn
        self._name = spec.name
        self._functions = frozenset(spec.functions)

    def __getattr__(self, attr: str):
        if attr.startswith("_") or attr not in self._functions:
            raise AttributeError(attr)

        def call(*args, **kwargs):
            self._conn.send(("call", "invoke", (self._name, attr, args, kwargs), {}))
            return _recv_result(self._conn)

        call.__name__ = attr
        return call

    def __dir__(self):
        return sorted(self._functions)
