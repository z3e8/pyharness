from __future__ import annotations

import multiprocessing as mp
import os
import pickle
import shutil
import signal
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from ...core.session_venv import SessionVenv
from ...tools.registry import _public_functions
from .child import child_main
from .linux_sandbox import linux_sandbox_supported
from .protocol import RemoteError, RemoteToolSpec, recv_json
from .sandbox import check_unsandboxed_platform, make_child_executable

# Serializes the set_executable -> Process.start critical section across every
# RemoteKernel in the process (see _start_locked).
_START_LOCK = threading.Lock()


@contextmanager
def _detached_main():
    """Stop the spawned child re-importing the *parent's* entry module.

    `multiprocessing.spawn.get_preparation_data` records how to rebuild
    `__main__` in the child, and the child replays it before anything else runs.
    That is wrong for us twice over: the child needs the harness, never the
    embedder's entry point, and the read jail deliberately refuses everything
    under $HOME except the interpreter and the `pyharness` package — so the
    re-import fails and the child dies before `child_main` is reached, surfacing
    as `(kernel process died — session state lost)` on the very first cell.

    Clearing `__spec__` and `__file__` leaves `get_preparation_data` with neither
    `init_main_from_name` nor `init_main_from_path` to emit. Console scripts and
    plain `python script.py` never hit this; nor does `python -m pkg`, because
    CPython's `_fixup_main_from_name` returns early for a name ending in
    `.__main__`. What breaks is `python -m pkg.module` — the shape `evals/run.py`
    and any embedder CLI take.

    Restored on the way out: `__main__` is process-global, and callers may still
    pickle objects defined there. Safe against concurrent starts because every
    caller holds `_START_LOCK`.
    """
    main = sys.modules.get("__main__")
    if main is None:
        yield
        return
    spec, had_file = main.__spec__, hasattr(main, "__file__")
    file = getattr(main, "__file__", None)
    main.__spec__ = None
    # Windows reads `__main__.__file__` unguarded when `__spec__` is None
    # (`spawn.py`'s `elif ... not main_module.__file__.endswith('.exe')`), so
    # None would raise there. sys.executable ends in `.exe` and makes that test
    # false, which skips the branch exactly as None does everywhere else.
    main.__file__ = sys.executable if sys.platform == "win32" else None
    try:
        yield
    finally:
        main.__spec__ = spec
        if had_file:
            main.__file__ = file
        else:
            del main.__file__


# Best-effort reaper of pyharness-sb-* temp dirs left by a previous run that
# crashed hard (close() removes them on a clean exit). Age-gated well past any
# realistic session length so a concurrently running session's dir is never
# swept; the sweep runs once per process.
_SANDBOX_DIR_MAX_AGE_S = 24 * 3600
_reaped_stale_sandboxes = False


def _reap_stale_sandbox_dirs() -> None:
    global _reaped_stale_sandboxes
    if _reaped_stale_sandboxes:
        return
    _reaped_stale_sandboxes = True
    cutoff = time.time() - _SANDBOX_DIR_MAX_AGE_S
    try:
        for entry in Path(tempfile.gettempdir()).glob("pyharness-sb-*"):
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass


# How long a resync waits for an interrupted cell to report done before giving
# up and killing the child (_resync_after_abort).
_DRAIN_DEADLINE_S = 5.0
# Per-stage join timeout in the terminate -> join -> kill -> join escalation
# (_reap) and for close()'s graceful shutdown join.
_REAP_TIMEOUT_S = 2.0


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
    next `run` starts a fresh child.

    A Ctrl-C mid-cell unwinds the parent out of `_serve` while the child keeps
    running; its buffered frames would otherwise be consumed as the *next*
    cell's traffic, shifting every later turn's output by one. `run` therefore
    resynchronizes on the way out (`_resync_after_abort`): forward SIGINT so the
    cell aborts in the child (namespace preserved, like an in-process
    KeyboardInterrupt) and drain the aborted cell's frames — or, if the child
    doesn't wind down, kill it so the next `run` starts fresh."""

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
        if sandbox:
            # Fail at construction (i.e. at session start, before any spend),
            # not at first run: on a platform with no OS sandbox, agent code
            # would execute unconfined, so refuse unless the user set the
            # explicit PYHARNESS_ALLOW_UNSANDBOXED opt-in (see sandbox.py).
            check_unsandboxed_platform()
        self._venv = venv
        self._workspace = workspace
        self._ctx = mp.get_context("spawn")
        self._proc = None
        self._conn = None
        self._sbdir = None

    def _start(self) -> None:
        _reap_stale_sandbox_dirs()  # once per process: clear dirs a hard crash leaked
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
                # macOS confines by wrapping the child's exec (`exe` above);
                # Linux has no launcher, so the child restricts itself.
                bool(self.sandbox) and exe is None and linux_sandbox_supported(),
            ),
            daemon=True,
        )
        # get_preparation_data runs inside start(), so the detach has to span the
        # call itself, not just Process construction.
        # get_preparation_data runs inside start(), so the detach has to span the
        # call itself, not just Process construction.
        with _detached_main():
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
        try:
            # Redacted like the in-process kernel's output: the child's cell
            # output crosses back verbatim, and a capability error's message —
            # already redacted before it crossed (see _redact_exc) — may have
            # been echoed by agent code; the session-wide mask is the backstop.
            return self.broker.redact(self._serve())
        except BaseException:
            # Anything that unwinds mid-cell — typically a KeyboardInterrupt
            # while blocked in recv or inside a broker call (dispatch has
            # already written its outcome record by the time it re-raises) —
            # leaves the child mid-cell and the pipe desynced. Resync before
            # re-raising so the next run doesn't consume stale frames.
            self._resync_after_abort()
            raise

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
            if not (
                isinstance(op, str)
                and isinstance(args, list)
                and isinstance(kwargs, dict)
            ):
                self._discard()
                return "(kernel protocol error — session state lost)"
            try:
                value = _seal_for_wire(self.broker.call_op(op, *args, **kwargs))
                reply = ("ok", value)
            except Exception as exc:  # noqa: BLE001 - errors cross back to the agent
                reply = ("err", _safe_exc(_redact_exc(exc, self.broker.redact)))
            try:
                self._conn.send(reply)
            except Exception as exc:  # noqa: BLE001 - e.g. unpicklable result
                self._conn.send(
                    ("err", RuntimeError(f"{op} result not transferable: {exc!r}"))
                )

    def _resync_after_abort(self) -> None:
        """Restore the request/reply pairing after the parent unwound out of
        `_serve` mid-cell. Forward SIGINT to the child — the cell aborts with a
        KeyboardInterrupt (namespace preserved, see child._run_cell) — then
        drain the aborted cell's remaining frames up to its terminal "done". If
        the signal can't be sent (non-POSIX) or the child doesn't report done in
        time (e.g. agent code swallowed the interrupt), kill and discard it so
        the next run starts a fresh child instead of reading stale frames."""
        proc = self._proc
        if proc is None:
            return
        if os.name == "posix" and proc.pid is not None and proc.is_alive():
            try:
                os.kill(proc.pid, signal.SIGINT)
            except OSError:
                pass
            else:
                if self._drain_to_done():
                    return
        self._discard()

    def _drain_to_done(self) -> bool:
        """Consume the aborted cell's leftover frames until its "done" arrives.
        Stale capability-call frames are dropped unanswered — the forwarded
        SIGINT unblocks the child from awaiting those replies. Returns False on
        deadline, EOF, or a garbage frame (caller kills the child)."""
        deadline = time.monotonic() + _DRAIN_DEADLINE_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                if not self._conn.poll(remaining):
                    return False
                msg = recv_json(self._conn)
            except (EOFError, OSError, ValueError):
                return False
            if isinstance(msg, list) and len(msg) == 2 and msg[0] == "done":
                return True

    def _reap(self) -> None:
        """Make sure the current child is dead *and* reaped (no zombie), whatever
        state it is in: terminate() -> join -> kill() -> join. SIGTERM first so a
        healthy child can run atexit teardown; SIGKILL if it is wedged in a cell
        or ignoring the signal. A join on an already-dead child just collects its
        exit status."""
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.is_alive():
                proc.terminate()
                proc.join(_REAP_TIMEOUT_S)
                if proc.is_alive():
                    proc.kill()  # SIGKILL: cannot be caught or ignored
                    proc.join(_REAP_TIMEOUT_S)
            else:
                proc.join(_REAP_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - best-effort teardown of a broken handle
            pass

    def _discard(self) -> None:
        """Tear down the current child — dead, wedged, or off-protocol — and its
        pipe so the next run starts fresh. Escalates terminate -> join -> kill so
        even a child ignoring SIGTERM is killed and reaped; keeps the sandbox dir
        for reuse by the replacement child."""
        self._reap()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        self._proc, self._conn = None, None

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.is_alive():
                    self._conn.send(("shutdown",))
                    self._proc.join(timeout=_REAP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - best-effort graceful path
                pass
            # Graceful or not, escalate until the child is dead and reaped —
            # a child wedged mid-cell never reads the shutdown message.
            self._discard()
        if self._sbdir is not None:
            shutil.rmtree(self._sbdir, ignore_errors=True)
            self._sbdir = None


def _redact_exc(exc: BaseException, redact) -> BaseException:
    """Keep an injected secret from crossing the pipe inside an exception.

    An exception's message or attributes can embed request content — an httpx
    error carries the full URL (query params included), `TimeoutExpired` the
    whole argv — and the child formats whatever arrives into its traceback,
    which becomes agent-visible cell output. The clean case (no resolved secret
    spelled anywhere in str/repr) passes through untouched, preserving the
    exception type for agent-side `except SomeError` handling. A tainted one is
    rebuilt as a `RemoteError` carrying the original type name and the redacted
    message — the type identity is traded away only when the alternative is
    leaking cleartext into the child process."""
    shown = f"{type(exc).__name__}: {exc}"
    if redact(shown) == shown and redact(repr(exc)) == repr(exc):
        return exc
    return RemoteError(redact(shown))


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
