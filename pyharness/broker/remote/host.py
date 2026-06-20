from __future__ import annotations

import multiprocessing as mp
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType

from ...core.session_venv import SessionVenv
from ...security.vault import DEFAULT_ENV_PREFIX, PASSPHRASE_ENV
from ...tools.registry import _public_functions
from .child import child_main
from .protocol import RemoteToolSpec, recv_json
from .sandbox import make_child_executable


class RemoteKernel:
    """Out-of-process kernel: a drop-in for `Kernel` that runs each cell in a
    restricted child process instead of in-process.

    The child holds the persistent namespace and proxy stubs; the parent keeps
    the broker (policy -> audit -> budget -> execute), vault, and LLM client.
    During a cell the parent blocks in `run`, servicing the child's capability
    calls one at a time — so approvals and budget pauses suspend the agent's
    script naturally (it is blocked on IPC), and secrets never enter the child.

    State lives in the child's namespace and is in-memory and disposable: if the
    child dies mid-session its variables are lost (no durable resume in V1). The
    next `run` starts a fresh child."""

    def __init__(
        self,
        broker,
        *,
        sandbox: bool = True,
        secret_env_prefixes: tuple[str, ...] = (DEFAULT_ENV_PREFIX,),
        secret_env_names: tuple[str, ...] = (PASSPHRASE_ENV,),
        venv: SessionVenv | None = None,
    ):
        self.broker = broker
        self.sandbox = sandbox
        self._secret_env = (secret_env_prefixes, secret_env_names)
        self._venv = venv
        self._ctx = mp.get_context("spawn")
        self._proc = None
        self._conn = None
        self._sbdir = None

    def _start(self) -> None:
        # Launch the child under the OS sandbox when enabled and supported.
        # set_executable mutates the spawn context's launcher, so we set it just
        # before each start (sequential) — to the sandbox wrapper, or back to the
        # real interpreter if sandboxing is off/unsupported.
        exe = None
        if self.sandbox or self._venv is not None:
            if self._sbdir is None:
                self._sbdir = tempfile.mkdtemp(prefix="pyharness-sb-")
        if self.sandbox:
            exe = make_child_executable(Path(self._sbdir))
        self._ctx.set_executable(exe or sys.executable)

        venv_site = None
        if self._venv is not None:
            self._venv.ensure_created(Path(self._sbdir))
            site = self._venv.site_packages()
            venv_site = str(site) if site is not None else None

        parent_conn, child_conn = self._ctx.Pipe()
        prefixes, names = self._secret_env
        proc = self._ctx.Process(
            target=child_main,
            args=(child_conn, self.broker.op_names(), prefixes, names, venv_site),
            daemon=True,
        )
        proc.start()
        child_conn.close()  # the child holds its own copy; drop ours
        self._proc, self._conn = proc, parent_conn

    def run(self, code: str) -> str:
        if self._proc is None or not self._proc.is_alive():
            self._start()
        self._conn.send(("run", code))
        return self._serve()

    def _serve(self) -> str:
        """Service the child's capability calls until the cell reports done."""
        while True:
            try:
                # The child is untrusted: decode its messages as JSON, never via
                # pickle, so a crafted payload cannot execute in this (privileged,
                # unsandboxed) parent. See protocol.py.
                msg = recv_json(self._conn)
            except (EOFError, OSError):
                return "(kernel process died — session state lost)"
            if msg[0] == "done":
                return msg[1]

            _, op, args, kwargs = msg  # ("call", op, args, kwargs)
            try:
                value = _seal_for_wire(self.broker.call_op(op, *args, **kwargs))
                reply = ("ok", value)
            except Exception as exc:  # noqa: BLE001 - errors cross back to the agent
                reply = ("err", exc)
            try:
                self._conn.send(reply)
            except Exception as exc:  # noqa: BLE001 - e.g. unpicklable result
                self._conn.send(("err", RuntimeError(f"{op} result not transferable: {exc!r}")))

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.is_alive():
                self._conn.send(("shutdown",))
                self._proc.join(timeout=2)
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        if self._proc.is_alive():
            self._proc.terminate()
        self._proc, self._conn = None, None
        if self._sbdir is not None:
            shutil.rmtree(self._sbdir, ignore_errors=True)
            self._sbdir = None


def _seal_for_wire(value):
    """Convert a parent-only result into something that can cross the pipe. Live
    tool modules become a `RemoteToolSpec` (rebuilt as a proxy module in the
    child); everything else passes through and is pickled as-is."""
    if isinstance(value, ModuleType):
        funcs = tuple(name for name, _ in _public_functions(value))
        return RemoteToolSpec(value.__name__.rsplit(".", 1)[-1], funcs)
    return value
