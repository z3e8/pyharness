from __future__ import annotations

import multiprocessing as mp
import pickle
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType

from ...core.session_venv import SessionVenv
from ...tools.registry import _public_functions
from .child import child_main
from .protocol import RemoteError, RemoteToolSpec, recv_json
from .sandbox import make_child_executable

# Serializes the set_executable -> Process.start critical section across every
# RemoteKernel in the process (see _start_locked).
_START_LOCK = threading.Lock()


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
        venv: SessionVenv | None = None,
        workspace=None,
    ):
        self.broker = broker
        self.sandbox = sandbox
        self._venv = venv
        self._workspace = workspace
        self._ctx = mp.get_context("spawn")
        self._proc = None
        self._conn = None
        self._sbdir = None

    def _start(self) -> None:
        with _START_LOCK:
            self._start_locked()

    def _start_locked(self) -> None:
        # Launch the child under the OS sandbox when enabled and supported.
        # set_executable mutates the spawn context's launcher — and
        # mp.get_context("spawn") is a per-method singleton shared by every
        # RemoteKernel in the process — so the set/start pair is serialized
        # behind _START_LOCK: spawned children start their kernels from
        # parent-side threads, and without the lock two concurrent starts could
        # launch with each other's sandbox wrapper. The executable is set to the
        # sandbox wrapper, or back to the real interpreter if sandboxing is
        # off/unsupported.
        exe = None
        workspace_dir = self._workspace.dir if self._workspace is not None else None
        if self.sandbox or self._venv is not None:
            if self._sbdir is None:
                self._sbdir = tempfile.mkdtemp(prefix="pyharness-sb-")
        if self.sandbox:
            exe = make_child_executable(Path(self._sbdir), workspace_dir)
        self._ctx.set_executable(exe or sys.executable)

        venv_site = None
        if self._venv is not None:
            self._venv.ensure_created(Path(self._sbdir))
            site = self._venv.site_packages()
            venv_site = str(site) if site is not None else None

        parent_conn, child_conn = self._ctx.Pipe()
        proc = self._ctx.Process(
            target=child_main,
            args=(
                child_conn,
                self.broker.op_names(),
                venv_site,
                str(workspace_dir) if workspace_dir is not None else None,
            ),
            daemon=True,
        )
        proc.start()
        child_conn.close()  # the child holds its own copy; drop ours
        self._proc, self._conn = proc, parent_conn

    def run(self, code: str) -> str:
        if self._proc is None or not self._proc.is_alive():
            self._start()
        try:
            self._conn.send(("run", code))
        except (BrokenPipeError, OSError):
            # The child can die between the liveness check and the send: the pipe
            # closes as the process tears down, a moment before it becomes
            # reapable, so is_alive() may still read True. Start fresh and resend.
            self._discard()
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
                # The child is gone. Drop the dead handle so the next run starts a
                # fresh child instead of sending down a broken pipe.
                self._discard()
                return "(kernel process died — session state lost)"
            except ValueError:
                # Undecodable frame (not valid JSON) from the untrusted child.
                # `json.JSONDecodeError` is a ValueError; treat a garbage frame as a
                # hostile/broken child rather than letting the decode error escape
                # into the parent's control flow.
                self._discard()
                return "(kernel protocol error — session state lost)"

            # Validate the frame shape before trusting it. A hostile child could
            # send a scalar, a wrong-arity list, or wrong-typed members; splatting
            # those (`*args`/`**kwargs`) or unpacking them would raise arbitrary
            # exceptions in this privileged parent. Anything off-protocol tears the
            # child down deterministically instead.
            tag = msg[0] if isinstance(msg, list) and msg else None
            if tag == "done" and len(msg) == 2:
                return msg[1]
            if tag != "call" or len(msg) != 4:
                self._discard()
                return "(kernel protocol error — session state lost)"
            _, op, args, kwargs = msg  # ("call", op, args, kwargs)
            if not (isinstance(op, str) and isinstance(args, list) and isinstance(kwargs, dict)):
                self._discard()
                return "(kernel protocol error — session state lost)"
            try:
                value = _seal_for_wire(self.broker.call_op(op, *args, **kwargs))
                reply = ("ok", value)
            except Exception as exc:  # noqa: BLE001 - errors cross back to the agent
                reply = ("err", _safe_exc(exc))
            try:
                self._conn.send(reply)
            except Exception as exc:  # noqa: BLE001 - e.g. unpicklable result
                self._conn.send(("err", RuntimeError(f"{op} result not transferable: {exc!r}")))

    def _discard(self) -> None:
        """Drop an already-dead child and its broken pipe so the next run starts
        fresh. Unlike close(), this makes no attempt to shut the child down (it is
        gone); it just reaps the handle and keeps the sandbox dir for reuse."""
        if self._proc is not None:
            self._proc.join(timeout=1)
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        self._proc, self._conn = None, None

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


def _safe_exc(exc: BaseException) -> BaseException:
    """Ensure a broker error can survive the pickle round-trip to the child.

    The child re-raises whatever it receives, but reconstructs it as
    `type(exc)(*exc.args)` (via `BaseException.__reduce__`). Exceptions whose
    `__init__` needs more than `args` — e.g. `anthropic.APIStatusError` — crash
    the child's `recv()` with a bare `TypeError`, hiding the real error. We probe
    the exact round-trip here and, if it fails, fall back to a `RemoteError`
    carrying the original type name and message. Ordinary exceptions (including
    `PermissionDenied`) round-trip untouched, so agent code can still catch them
    by type."""
    try:
        pickle.loads(pickle.dumps(exc))
    except Exception:  # noqa: BLE001 - any reconstruction failure means "wrap it"
        return RemoteError(f"{type(exc).__name__}: {exc}")
    return exc


def _seal_for_wire(value):
    """Convert a parent-only result into something that can cross the pipe. Live
    tool modules become a `RemoteToolSpec` (rebuilt as a proxy module in the
    child); everything else passes through and is pickled as-is."""
    if isinstance(value, ModuleType):
        funcs = tuple(name for name, _ in _public_functions(value))
        return RemoteToolSpec(value.__name__.rsplit(".", 1)[-1], funcs)
    return value
