"""Out-of-process kernel: agent code runs in a child process, every capability
call crosses IPC back to the parent's broker. The agent code in these cells is
byte-for-byte what the in-process kernel runs — only the execution boundary moves.
"""

import json
import multiprocessing as mp
import os
import pickle
import threading
import time
from pathlib import Path

import pytest

from pyharness import Budget, Policy, Workspace
from pyharness.audit import AuditLog
from pyharness.broker import Broker
from pyharness.broker.capabilities import (
    FilesCapability,
    LLMCapability,
    ToolsCapability,
)
from pyharness.broker.remote import RemoteKernel
from pyharness.broker.remote import host as remote_host
from pyharness.broker.remote.protocol import recv_json
from pyharness.broker.remote.sandbox import macos_sandbox_supported
from pyharness.llm.client import Completion
from pyharness.tools.registry import Registry

requires_sandbox = pytest.mark.skipif(
    not macos_sandbox_supported(), reason="OS sandbox only built for macOS"
)


class StubLLM:
    """A worker that always succeeds, so map_llm results are deterministic."""

    def complete(
        self,
        *,
        system,
        messages,
        tier="cheap",
        tools=None,
        max_tokens=None,
        on_token=None,
        on_thinking=None,
        on_attempt=None,
        cache_anchor=None,
        total_deadline_s=None,
    ):
        return Completion(text="worked", tool_calls=[], content=[])


class _KwOnlyError(Exception):
    """Mimics `anthropic.APIStatusError`: `__init__` has required keyword-only
    args, so `BaseException.__reduce__` -> `type(exc)(*exc.args)` cannot rebuild
    it. Unpickled naively in the child, it raises a bare `TypeError` that masks
    the real message."""

    def __init__(self, message, *, response, body):
        super().__init__(message)
        self.response = response
        self.body = body


class BoomCapability:
    """A core capability whose one op raises `_KwOnlyError`, to exercise the
    parent->child error-marshalling path."""

    name = "boom"

    def exports(self):
        return {"boom": self._boom}

    def _boom(self):
        raise _KwOnlyError("api error 400: bad tool version", response="r", body="b")


class InterruptCapability:
    """A capability whose one op raises KeyboardInterrupt parent-side — standing
    in for a real Ctrl-C landing while the parent blocks in `_serve` (the child
    is deterministically mid-cell, awaiting the call's reply)."""

    name = "intr"

    def exports(self):
        return {"interrupt_parent": self._interrupt}

    def _interrupt(self):
        raise KeyboardInterrupt


def _example_tool():
    """A throwaway registry tool: a module named `widget` exposing `double`."""
    from types import ModuleType

    module = ModuleType("widget")
    module.__doc__ = "Widget helpers."

    def double(n):
        return n * 2

    double.__module__ = "widget"  # so _public_functions discovers it
    module.double = double
    return module


def _broker(tmp_path, policy=None, *, with_agents=False):
    ws = Workspace(tmp_path)
    broker = Broker(policy or Policy(), AuditLog(tmp_path / "audit.jsonl"), Budget())
    broker.register(FilesCapability(ws))
    registry = Registry()
    registry.register(_example_tool(), source="installed")
    broker.register(ToolsCapability(registry))
    if with_agents:
        broker.register(LLMCapability(StubLLM()))
    return broker


@pytest.fixture
def kernel_factory():
    kernels = []

    def make(broker, **kwargs):
        k = RemoteKernel(broker, **kwargs)
        kernels.append(k)
        return k

    yield make
    for k in kernels:
        k.close()


def test_variables_persist_across_cells(kernel_factory, tmp_path):
    kernel = kernel_factory(_broker(tmp_path))
    assert kernel.run("n = 41") == "(no output)"
    # Pure compute stays in the child; only the print returns.
    assert kernel.run("print(n + 1)") == "42"


def test_capability_routes_to_parent_broker_and_audits(kernel_factory, tmp_path):
    broker = _broker(tmp_path)
    kernel = kernel_factory(broker)
    out = kernel.run("write('note.txt', 'data')\nprint(read('note.txt'))")
    assert out == "data"
    # The side effect happened parent-side: file on disk + audit entry.
    assert (Workspace(tmp_path).dir / "note.txt").read_text() == "data"
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    actions = [json.loads(line).get("action") for line in lines]
    assert "files.write" in actions and "files.read" in actions


def test_policy_denial_surfaces_as_traceback(kernel_factory, tmp_path):
    broker = _broker(tmp_path, policy=Policy(deny={"files"}))
    kernel = kernel_factory(broker)
    out = kernel.run("write('x.txt', 'y')")
    assert "PermissionDenied" in out


def test_policy_denial_is_catchable_by_agent_code(kernel_factory, tmp_path):
    broker = _broker(tmp_path, policy=Policy(deny={"files"}))
    kernel = kernel_factory(broker)
    out = kernel.run(
        "from pyharness.broker import PermissionDenied\n"
        "try:\n"
        "    write('x.txt', 'y')\n"
        "except PermissionDenied:\n"
        "    print('blocked')\n"
    )
    assert out == "blocked"


def test_use_tool_remote_module(kernel_factory, tmp_path):
    kernel = kernel_factory(_broker(tmp_path))
    out = kernel.run("widget = use_tool('widget')\nprint(widget.double(21))")
    assert out == "42"
    # The tool call routed through the broker as tools.invoke.
    actions = [
        json.loads(line).get("action")
        for line in (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    ]
    assert "tools.invoke" in actions


def test_map_llm_returns_results(kernel_factory, tmp_path):
    kernel = kernel_factory(_broker(tmp_path, with_agents=True))
    out = kernel.run(
        "rs = map_llm(['a', 'b', 'c'])\nprint(sum(r.ok for r in rs), rs[0].value)"
    )
    assert out == "3 worked"


def test_child_cwd_is_workspace(kernel_factory, tmp_path):
    # With a workspace, the child chdirs into it, so raw Python and the `files`
    # builtins agree on what a relative path means: a broker write lands in the
    # workspace and a bare `open(name)` reads it back.
    ws = Workspace(tmp_path)
    kernel = kernel_factory(_broker(tmp_path), workspace=ws)
    out = kernel.run(
        "write('f.txt', 'hi')\n"
        "import os\n"
        "print(os.path.basename(os.getcwd()), open('f.txt').read())\n"
    )
    assert out == "workspace hi"


@requires_sandbox
def test_sandbox_allows_workspace_writes_but_denies_escape(kernel_factory, tmp_path):
    # "The workspace is the sandbox": raw writes *inside* the workspace succeed, so
    # libraries that persist files just work — but a write one step outside it is
    # still denied by the OS.
    ws = Workspace(tmp_path)
    kernel = kernel_factory(_broker(tmp_path), workspace=ws)
    wrote = kernel.run(
        "f = open('scratch.txt', 'w'); f.write('hi'); f.close()\n"
        "print(open('scratch.txt').read())\n"
    )
    assert wrote == "hi"
    assert (ws.dir / "scratch.txt").read_text() == "hi"
    denied = kernel.run(
        "try:\n"
        "    open('../escape.txt', 'w').write('x')\n"
        "    print('WROTE')\n"
        "except OSError:\n"
        "    print('denied')\n"
    )
    assert denied == "denied"
    assert not (tmp_path / "escape.txt").exists()


@requires_sandbox
def test_sandbox_read_jail_denies_home_files(kernel_factory, tmp_path):
    # The read jail hides the user's personal files: a file under $HOME the agent
    # was never handed is unreadable, even though the child can still read its
    # workspace, the interpreter, and its own package source (or it couldn't import
    # pyharness to start at all).
    probe = Path.home() / ".pyharness_readjail_probe"
    probe.write_text("secret")
    try:
        ws = Workspace(tmp_path)
        kernel = kernel_factory(_broker(tmp_path), workspace=ws)
        out = kernel.run(
            f"try:\n"
            f"    print('READ', open({str(probe)!r}).read())\n"
            f"except OSError:\n"
            f"    print('denied')\n"
        )
        assert out == "denied"
    finally:
        probe.unlink(missing_ok=True)


@requires_sandbox
def test_sandbox_read_jail_allows_package_but_not_repo_neighbours(
    kernel_factory, tmp_path
):
    # The jail keeps only what the interpreter needs readable. The pyharness
    # package imports fine, but sibling files under the repo root that Python never
    # needs — the project's own source outside the package, its .env — are denied,
    # even though the repo root must be *listable* to resolve the import.
    import pyharness

    pkg_dir = Path(pyharness.__file__).resolve().parent  # <repo>/pyharness
    repo_root = pkg_dir.parent
    ws = Workspace(tmp_path)
    kernel = kernel_factory(_broker(tmp_path), workspace=ws)
    out = kernel.run(
        "import pyharness, os\n"
        f"print('import', bool(pyharness.__file__))\n"
        f"print('listable', 'pyharness' in os.listdir({str(repo_root)!r}))\n"
        "def readable(p):\n"
        "    try:\n"
        "        open(p).read(); return True\n"
        "    except OSError:\n"
        "        return False\n"
        f"print('pkg', readable({str(pkg_dir / '__init__.py')!r}))\n"
        f"print('readme', readable({str(repo_root / 'README.md')!r}))\n"
        f"print('dotenv', readable({str(repo_root / '.env')!r}))\n"
    )
    assert out.splitlines() == [
        "import True",
        "listable True",
        "pkg True",
        "readme False",
        "dotenv False",
    ]


@requires_sandbox
@pytest.mark.parametrize("where", ["home", "temp"])
def test_sandbox_denies_filesystem_writes(kernel_factory, tmp_path, where):
    # The child reaches around the broker and writes straight to disk. The OS
    # sandbox denies *every* write — both a sensitive user path (home) and an
    # ephemeral one (temp). The temp case matters most: such a file would be
    # invisible to the agent and the human and bypass the broker, so it is denied
    # deliberately rather than waved through as "just scratch".
    kernel = kernel_factory(_broker(tmp_path))
    # tmp_path lives under the OS temp root (/var/folders); home is a real file.
    escape = (
        (Path.home() / ".pyharness_sandbox_escape_test")
        if where == "home"
        else (tmp_path / "escape.txt")
    )
    out = kernel.run(
        f"try:\n"
        f"    open({str(escape)!r}, 'w').write('x')\n"
        f"    print('WROTE')\n"
        f"except OSError as e:\n"
        f"    print('denied', e.errno)\n"
    )
    assert out.startswith("denied")
    assert not escape.exists()


@requires_sandbox
def test_child_can_spawn_subprocess(kernel_factory, tmp_path):
    # RLIMIT_NPROC is per-user on macOS, so the old 512 cap made every fork() —
    # subprocess included — fail with EAGAIN once the desktop crossed that many
    # processes. Skipped on Darwin now, so a plain subprocess runs (under the
    # inherited Seatbelt profile: reading /bin/echo and forking are both fine).
    ws = Workspace(tmp_path)
    kernel = kernel_factory(_broker(tmp_path), workspace=ws)
    out = kernel.run(
        "import subprocess\n"
        "r = subprocess.run(['/bin/echo', 'hi'], capture_output=True, text=True)\n"
        "print(r.stdout.strip())\n"
    )
    assert out == "hi"


@requires_sandbox
def test_sandbox_denies_outbound_network(kernel_factory, tmp_path):
    kernel = kernel_factory(_broker(tmp_path))
    out = kernel.run(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('denied')\n"
    )
    assert out == "denied"


@requires_sandbox
def test_sandbox_still_allows_broker_writes_and_compute(kernel_factory, tmp_path):
    # The sandbox blocks the child's own writes, not the broker's: `write()`
    # executes parent-side (unsandboxed), and pure compute is untouched.
    kernel = kernel_factory(_broker(tmp_path))
    out = kernel.run(
        "write('note.txt', 'data')\nprint(read('note.txt'), sum(range(5)))"
    )
    assert out == "data 10"
    assert (Workspace(tmp_path).dir / "note.txt").read_text() == "data"


def test_parent_never_unpickles_child_bytes(tmp_path):
    # Regression for the IPC trust inversion: the child is untrusted, so the
    # parent decodes its messages as JSON, never pickle. A pickle whose
    # __reduce__ runs code — exactly what a hostile child would craft — must fail
    # to decode in the parent rather than execute.
    parent_conn, child_conn = mp.get_context("spawn").Pipe()
    sentinel = tmp_path / "pwned"

    class Exploit:
        def __reduce__(self):
            import os

            return (os.system, (f"touch {sentinel}",))

    child_conn.send_bytes(pickle.dumps(Exploit()))
    with pytest.raises(Exception):
        recv_json(parent_conn)
    assert not sentinel.exists()


def test_broker_error_with_kwonly_init_surfaces_real_message(kernel_factory, tmp_path):
    # Regression: a broker error whose __init__ has required keyword-only args
    # (e.g. anthropic.APIStatusError) once crashed the child's recv() with a bare
    # "missing arguments" TypeError, masking the real failure. The parent now
    # normalizes it so the true message reaches the failing cell.
    broker = _broker(tmp_path)
    broker.register(BoomCapability())
    kernel = kernel_factory(broker)
    out = kernel.run("boom()")
    assert "api error 400: bad tool version" in out
    assert "_KwOnlyError" in out
    assert "missing" not in out  # not the masked reconstruction TypeError


def test_non_transferable_argument_fails_cleanly(kernel_factory, tmp_path):
    # An argument that can't cross the JSON wire surfaces a clear error in the
    # cell, not a crashed child or a silent pickle.
    kernel = kernel_factory(_broker(tmp_path))
    out = kernel.run("write('x.txt', {1, 2, 3})")
    assert "not transferable" in out


def test_child_environment_has_no_secrets(kernel_factory, tmp_path, monkeypatch):
    # Env-backed secrets and the file-vault passphrase live in the parent's
    # environment; the spawned child inherits it. The child reduces its copy to
    # the minimal allowlist before any cell runs, so the agent cannot read
    # cleartext — or any unknown .env var — from os.environ.
    monkeypatch.setenv("PYHARNESS_SECRET_TOKEN", "supersecret")
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "hunter2")
    monkeypatch.setenv("NOT_LISTED", "dropped")  # default-deny: unknown vars go too
    kernel = kernel_factory(_broker(tmp_path))
    out = kernel.run(
        "import os\n"
        "print(os.environ.get('PYHARNESS_SECRET_TOKEN'))\n"
        "print(os.environ.get('PYHARNESS_VAULT_PASSPHRASE'))\n"
        "print(os.environ.get('NOT_LISTED'))\n"
        "print('HOME' in os.environ)\n"
    )
    assert out == "None\nNone\nNone\nTrue"


requires_posix_signals = pytest.mark.skipif(
    os.name != "posix", reason="SIGINT/SIGTERM forwarding is POSIX-only"
)


@requires_posix_signals
def test_interrupt_mid_cell_resyncs_pipe_and_keeps_state(kernel_factory, tmp_path):
    # Regression for the Ctrl-C desync: a KeyboardInterrupt unwinding the parent
    # mid-cell used to leave the child's buffered "done" in the pipe, where the
    # *next* run consumed it as its own result — shifting every later turn's
    # output by one. The resync forwards SIGINT (cell aborts child-side) and
    # drains the aborted cell's frames, so the next cell's result is its own and
    # the namespace survives, like an in-process interrupt.
    broker = _broker(tmp_path)
    broker.register(InterruptCapability())
    kernel = kernel_factory(broker)
    kernel.run("keep = 41")
    with pytest.raises(KeyboardInterrupt):
        kernel.run("interrupt_parent()\nprint('UNREACHED')")
    out = kernel.run("print(keep + 1)")
    assert out == "42"  # not the interrupted cell's stale traceback/output
    assert "UNREACHED" not in out


@requires_posix_signals
def test_interrupt_swallowed_by_agent_code_falls_back_to_kill(
    kernel_factory, tmp_path, monkeypatch
):
    # If agent code swallows the forwarded interrupt and never reports done, the
    # drain times out and the child is killed — the next run starts fresh
    # instead of hanging or reading stale frames.
    monkeypatch.setattr(remote_host, "_DRAIN_DEADLINE_S", 1.0)
    monkeypatch.setattr(remote_host, "_REAP_TIMEOUT_S", 0.5)
    broker = _broker(tmp_path)
    broker.register(InterruptCapability())
    kernel = kernel_factory(broker)
    kernel.run("x = 1")
    proc = kernel._proc
    with pytest.raises(KeyboardInterrupt):
        kernel.run(
            "try:\n"
            "    interrupt_parent()\n"
            "except BaseException:\n"
            "    import time\n"
            "    time.sleep(60)\n"
        )
    assert not proc.is_alive()
    assert kernel._proc is None
    # Fresh child: pipe is clean, prior state gone.
    assert "NameError" in kernel.run("print(x)")
    assert kernel.run("print('fresh')") == "fresh"


@requires_posix_signals
def test_discard_kills_and_reaps_wedged_child(kernel_factory, tmp_path, monkeypatch):
    # _discard must actually tear a live child down, escalating past an ignored
    # SIGTERM to SIGKILL, and reap it (exit status collected — no zombie).
    monkeypatch.setattr(remote_host, "_REAP_TIMEOUT_S", 0.5)
    kernel = kernel_factory(_broker(tmp_path))
    kernel.run("import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    proc = kernel._proc
    assert proc.is_alive()
    kernel._discard()
    assert not proc.is_alive()
    assert proc.exitcode is not None  # reaped, not a zombie
    assert kernel._proc is None and kernel._conn is None


@requires_posix_signals
def test_close_kills_and_reaps_child_wedged_mid_cell(tmp_path, monkeypatch):
    # close() on a child stuck mid-cell (it never reads the shutdown message,
    # and here also ignores SIGTERM) must escalate to SIGKILL and reap.
    monkeypatch.setattr(remote_host, "_REAP_TIMEOUT_S", 0.5)
    kernel = RemoteKernel(_broker(tmp_path))
    kernel.run("import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    proc = kernel._proc
    entered = threading.Event()

    def wedge():
        entered.set()
        kernel.run("import time; time.sleep(60)")

    t = threading.Thread(target=wedge, daemon=True)
    t.start()
    entered.wait(timeout=5)
    time.sleep(0.3)  # let the child enter the cell
    kernel.close()
    assert not proc.is_alive()
    assert proc.exitcode is not None  # reaped, not a zombie
    t.join(timeout=5)
    assert not t.is_alive()


def test_close_reaps_idle_child(kernel_factory, tmp_path):
    # The graceful path: an idle child honors the shutdown message; close()
    # still collects its exit status so nothing is left for the OS to hold.
    kernel = kernel_factory(_broker(tmp_path))
    kernel.run("x = 1")
    proc = kernel._proc
    kernel.close()
    assert not proc.is_alive()
    assert proc.exitcode is not None


def test_child_restarts_after_crash(kernel_factory, tmp_path):
    kernel = kernel_factory(_broker(tmp_path))
    kernel.run("keep = 1")
    # A hard exit kills the child mid-session; state is lost (no durable resume).
    crashed = kernel.run("import os; os._exit(1)")
    assert "died" in crashed
    # The next cell transparently starts a fresh child — but `keep` is gone.
    assert "NameError" in kernel.run("print(keep)")
    assert kernel.run("print('alive')") == "alive"
