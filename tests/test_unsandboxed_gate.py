"""Off macOS there is no OS-level confinement at all (2026-07 readiness M8):
agent code would run with the parent user's full filesystem and network reach.
pyharness therefore refuses to start a kernel on such a platform by default;
PYHARNESS_ALLOW_UNSANDBOXED=true opts in explicitly, with a one-time loud
stderr warning. Platform support is mocked both ways so these tests exercise
the gate on any host OS."""

from __future__ import annotations

import pytest

from pyharness.broker.remote import sandbox
from pyharness.broker.remote.host import RemoteKernel
from pyharness.broker.remote.sandbox import (
    ALLOW_UNSANDBOXED_ENV,
    SandboxWithoutWorkspaceError,
    UnsandboxedPlatformError,
    check_unsandboxed_platform,
)


@pytest.fixture
def no_os_sandbox(monkeypatch):
    """Pretend this platform has no OS sandbox, with the opt-in unset and the
    one-time warning re-armed. monkeypatch restores all three afterwards."""
    monkeypatch.setattr(sandbox, "os_sandbox_supported", lambda: False)
    monkeypatch.delenv(ALLOW_UNSANDBOXED_ENV, raising=False)
    monkeypatch.setattr(sandbox, "_UNSANDBOXED_WARNED", False)


def test_refuses_by_default_and_names_the_opt_in(no_os_sandbox):
    with pytest.raises(UnsandboxedPlatformError) as excinfo:
        check_unsandboxed_platform()
    # The error must be actionable: name the opt-in var and the doc to read.
    assert ALLOW_UNSANDBOXED_ENV in str(excinfo.value)
    assert "security-and-audit" in str(excinfo.value)


def test_opt_in_proceeds_with_one_time_warning(no_os_sandbox, monkeypatch, capsys):
    monkeypatch.setenv(ALLOW_UNSANDBOXED_ENV, "true")
    check_unsandboxed_platform()  # no raise
    err = capsys.readouterr().err
    assert "UNCONFINED" in err and ALLOW_UNSANDBOXED_ENV in err
    # A second kernel (e.g. a spawned child's) must not repeat the warning.
    check_unsandboxed_platform()
    assert capsys.readouterr().err == ""


def test_sandboxed_platform_is_untouched(monkeypatch, capsys):
    # With an OS sandbox available (macOS today): no gate, no warning, and the
    # opt-in var — even if set — changes nothing.
    monkeypatch.setattr(sandbox, "os_sandbox_supported", lambda: True)
    monkeypatch.delenv(ALLOW_UNSANDBOXED_ENV, raising=False)
    check_unsandboxed_platform()
    monkeypatch.setenv(ALLOW_UNSANDBOXED_ENV, "true")
    check_unsandboxed_platform()
    assert capsys.readouterr().err == ""


def test_remote_kernel_gates_at_construction(no_os_sandbox):
    # The seam: Session builds a RemoteKernel in its constructor, so the refusal
    # lands at session start — before any child spawns or LLM spend.
    with pytest.raises(UnsandboxedPlatformError):
        RemoteKernel(object())


def test_remote_kernel_constructs_under_opt_in(no_os_sandbox, monkeypatch, capsys):
    monkeypatch.setenv(ALLOW_UNSANDBOXED_ENV, "1")
    kernel = RemoteKernel(object())  # no raise; no child started yet
    assert kernel._proc is None
    assert "UNCONFINED" in capsys.readouterr().err


def test_sandboxed_kernel_refuses_without_a_workspace(monkeypatch):
    # The second construction gate: where a real OS sandbox will confine the
    # child, it must have a workspace to scope the write allowance and $HOME read
    # jail to. Building one without a workspace would (on macOS) silently drop
    # the read jail while keeping the network and write denials — the one silent
    # sandbox degradation — so it is refused as loudly as the platform gate.
    monkeypatch.setattr(sandbox, "os_sandbox_supported", lambda: True)
    with pytest.raises(SandboxWithoutWorkspaceError):
        RemoteKernel(object())  # sandbox=True default, no workspace
    # Scope of the refusal: sandbox=False has no jail to scope, so the workspace
    # is not required there even on a sandbox-capable platform.
    kernel = RemoteKernel(object(), sandbox=False)
    assert kernel._proc is None
