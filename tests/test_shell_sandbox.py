"""shell.bash executes parent-side, so without OS confinement it would run with
the parent's full reach (2026-07 readiness C1). On macOS it now runs under the
same Seatbelt profile as the out-of-process child: workspace writes work, but
outbound network, writes outside the workspace, and $HOME reads are denied at
the OS level. Where no OS sandbox exists, bash falls back to the env-scrubbed
plain run (a Linux path plugs into `sandboxed_shell_argv` later)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pyharness import Workspace
from pyharness.broker.capabilities.shell import ShellCapability
from pyharness.broker.remote import sandbox
from pyharness.broker.remote.sandbox import (
    _seatbelt_profile,
    macos_sandbox_supported,
    sandboxed_shell_argv,
)

requires_sandbox = pytest.mark.skipif(
    not macos_sandbox_supported(), reason="OS sandbox only built for macOS"
)


@requires_sandbox
def test_shell_argv_wraps_command_in_the_child_profile(tmp_path):
    # The wrapper is the child's own Seatbelt profile — same builder, not a
    # fork — and the generated profile lives outside the workspace, where a
    # sandboxed command has no write access to loosen its own jail.
    ws = Workspace(tmp_path)
    argv = sandboxed_shell_argv("echo hi", ws.root, ws.dir)
    assert argv[0] == "/usr/bin/sandbox-exec" and argv[1] == "-f"
    assert argv[3:] == ["/bin/sh", "-c", "echo hi"]
    profile = Path(argv[2])
    assert profile.read_text() == _seatbelt_profile(ws.dir)
    assert not profile.is_relative_to(ws.dir)


@requires_sandbox
def test_bash_writes_inside_workspace_but_not_outside(tmp_path):
    # "The workspace is the sandbox": writes inside it succeed, one step above
    # it (the session root) they are denied by the OS.
    ws = Workspace(tmp_path)
    bash = ShellCapability(ws).bash
    assert bash("echo hi > note.txt && cat note.txt").strip() == "hi"
    assert (ws.dir / "note.txt").read_text().strip() == "hi"
    escape = tmp_path / "escape.txt"
    bash(f"echo x > {escape}")
    assert not escape.exists()


@requires_sandbox
def test_bash_denied_outbound_network(tmp_path):
    ws = Workspace(tmp_path)
    bash = ShellCapability(ws).bash
    (ws.dir / "probe_net.py").write_text(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('denied')\n"
    )
    assert bash(f"{sys.executable} probe_net.py").strip() == "denied"


@requires_sandbox
def test_bash_read_jail_denies_home_files(tmp_path):
    probe = Path.home() / ".pyharness_shell_readjail_probe"
    probe.write_text("secret")
    try:
        out = ShellCapability(Workspace(tmp_path)).bash(
            f"cat {probe} 2>/dev/null || echo denied"
        )
        assert "secret" not in out
        assert "denied" in out
    finally:
        probe.unlink(missing_ok=True)


def test_bash_falls_back_to_plain_run_without_os_sandbox(tmp_path, monkeypatch):
    # Where the platform has no OS sandbox, sandboxed_shell_argv returns None
    # and bash runs as before — env scrubbing the only containment. This state
    # is only reachable behind the PYHARNESS_ALLOW_UNSANDBOXED opt-in: the
    # session's kernel refuses to start unsandboxed without it (see
    # test_unsandboxed_gate.py), so bash itself needs no second gate.
    monkeypatch.setattr(sandbox, "macos_sandbox_supported", lambda: False)
    out = ShellCapability(Workspace(tmp_path)).bash("echo hi")
    assert out.strip() == "hi"
