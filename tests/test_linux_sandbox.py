"""Linux enforces the same three invariants as the macOS Seatbelt profile — no
outbound network, no write outside the workspace, no $HOME reads — through
Landlock (filesystem) plus a seccomp-bpf filter (network). Both are applied by
the process to itself, need no helper binary or user namespaces, and survive
exec.

Two layers of test here, deliberately:

* The seccomp filter's failure mode is *silent non-enforcement* — a wrong
  seccomp_data offset or an inverted jump yields a program that installs cleanly
  and blocks nothing. So the BPF program is interpreted directly against
  synthetic syscall records, which runs on every platform including the macOS CI
  leg, rather than only being exercised where it can be installed.
* The real-kernel tests then confirm the syscalls actually behave. They probe
  raw `socket()` with explicit address families, never urllib: a test that only
  checked "an HTTP call fails" would pass against a filter that blocks nothing,
  because the broker's egress guard would take the blame.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pyharness.broker.remote.linux_sandbox import (
    _AF_INET,
    _AF_INET6,
    _AF_PACKET,
    _ARCH,
    _SECCOMP_RET_ALLOW,
    _SECCOMP_RET_ERRNO,
    _SECCOMP_RET_KILL_PROCESS,
    MIN_LANDLOCK_ABI,
    _network_deny_filter,
    landlock_abi,
    linux_sandbox_supported,
)

requires_linux_sandbox = pytest.mark.skipif(
    not linux_sandbox_supported(),
    reason=f"needs Linux with Landlock ABI >= {MIN_LANDLOCK_ABI}",
)

AUDIT_ARCH_X86_64, NR_SOCKET_X86_64 = _ARCH["x86_64"]

_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_RET_K = 0x06

AF_UNIX = 1


def run_filter(program, *, arch: int, nr: int, arg0: int = 0) -> int:
    """Interpret the classic-BPF program against one synthetic seccomp_data
    record and return the verdict. Faithful to the three instruction classes the
    filter uses; anything else is a bug in the filter, not the interpreter."""
    data = {0: nr, 4: arch, 16: arg0}
    acc, pc, steps = 0, 0, 0
    while True:
        steps += 1
        assert steps < 1000, "filter did not terminate"
        ins = program[pc]
        if ins.code == _BPF_LD_W_ABS:
            assert ins.k in data, f"loads unmodelled seccomp_data offset {ins.k}"
            acc = data[ins.k]
            pc += 1
        elif ins.code == _BPF_JEQ_K:
            pc += 1 + (ins.jt if acc == ins.k else ins.jf)
        elif ins.code == _BPF_RET_K:
            return ins.k
        else:
            raise AssertionError(f"unknown BPF opcode {ins.code:#x}")


@pytest.fixture
def program():
    return _network_deny_filter(AUDIT_ARCH_X86_64, NR_SOCKET_X86_64)


@pytest.mark.parametrize("family", [_AF_INET, _AF_INET6, _AF_PACKET])
def test_socket_to_the_network_is_denied(program, family):
    verdict = run_filter(
        program, arch=AUDIT_ARCH_X86_64, nr=NR_SOCKET_X86_64, arg0=family
    )
    assert verdict == _SECCOMP_RET_ERRNO | 1  # EPERM


def test_af_unix_stays_allowed(program):
    # AF_UNIX reaches nothing off-box and denying it breaks ordinary library
    # behaviour; its filesystem side is governed by Landlock instead.
    verdict = run_filter(
        program, arch=AUDIT_ARCH_X86_64, nr=NR_SOCKET_X86_64, arg0=AF_UNIX
    )
    assert verdict == _SECCOMP_RET_ALLOW


def test_unrelated_syscalls_are_untouched(program):
    # A syscall whose *first argument* happens to equal AF_INET must not be
    # mistaken for a socket() call — the filter has to check the number first.
    verdict = run_filter(program, arch=AUDIT_ARCH_X86_64, nr=1, arg0=_AF_INET)
    assert verdict == _SECCOMP_RET_ALLOW


def test_foreign_abi_is_killed_not_allowed(program):
    # Entering through a different ABI (x32 on x86_64) means the same syscall
    # number denotes something else, so the filter cannot reason about the
    # arguments. It must fail closed rather than fall through to ALLOW.
    verdict = run_filter(program, arch=0xC000003E ^ 0xFF, nr=NR_SOCKET_X86_64, arg0=0)
    assert verdict == _SECCOMP_RET_KILL_PROCESS


def test_every_supported_arch_has_a_coherent_filter():
    for machine, (audit_arch, nr_socket) in _ARCH.items():
        prog = _network_deny_filter(audit_arch, nr_socket)
        assert (
            run_filter(prog, arch=audit_arch, nr=nr_socket, arg0=_AF_INET)
            == _SECCOMP_RET_ERRNO | 1
        ), machine
        assert (
            run_filter(prog, arch=audit_arch, nr=nr_socket, arg0=AF_UNIX)
            == _SECCOMP_RET_ALLOW
        ), machine


def test_supported_predicate_is_false_off_linux():
    if sys.platform != "linux":
        assert not linux_sandbox_supported()
        assert landlock_abi() == 0


# --- real-kernel enforcement -------------------------------------------------
#
# These run in a subprocess: the restrictions are irrevocable, so applying them
# in the pytest process would confine the rest of the suite.

_PROBE = """
import os, socket, subprocess, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from pyharness.broker.remote.linux_sandbox import apply_linux_sandbox

ws, outside = Path({ws!r}), Path({outside!r})
apply_linux_sandbox(ws, ())
results = {{}}

def probe(name, fn):
    try:
        fn()
        results[name] = "allowed"
    except OSError as exc:
        results[name] = type(exc).__name__

probe("tcp", lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
probe("udp", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
probe("tcp6", lambda: socket.socket(socket.AF_INET6, socket.SOCK_STREAM))
probe("unix", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
probe("write_in", lambda: (ws / "a").write_text("a"))
probe("write_out", lambda: (outside / "b").write_text("b"))
probe("read_home", lambda: (Path.home() / ".pyharness_probe").read_text())
probe("read_etc", lambda: Path("/etc/hostname").read_text())
probe("read_proc", lambda: Path("/proc/meminfo").read_text())
probe("import", lambda: __import__("csv"))
probe("parent_environ", lambda: Path(f"/proc/{{os.getppid()}}/environ").read_bytes())

# No escape by exec: a child process inherits both restrictions.
r = subprocess.run([sys.executable, "-c",
    "import socket;socket.socket(socket.AF_INET,socket.SOCK_STREAM);print('ESCAPED')"],
    capture_output=True, text=True)
results["exec_tcp"] = "allowed" if "ESCAPED" in r.stdout else "blocked"

print(repr(results))
"""


@pytest.fixture(scope="module")
def enforced(tmp_path_factory):
    """Apply the sandbox in a child process and report what each probe did."""
    ws = tmp_path_factory.mktemp("ws")
    outside = tmp_path_factory.mktemp("outside")
    (Path.home() / ".pyharness_probe").write_text("SECRET")
    code = _PROBE.format(
        repo=str(Path(__file__).resolve().parents[1]),
        ws=str(ws),
        outside=str(outside),
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return eval(proc.stdout.strip())  # noqa: S307 - our own repr, not input


@requires_linux_sandbox
@pytest.mark.parametrize("probe", ["tcp", "udp", "tcp6"])
def test_outbound_network_is_denied_by_family(enforced, probe):
    # The invariant the broker cannot enforce: raw Python never touches dispatch,
    # so without this the egress guard is simply not in the path.
    assert enforced[probe] == "PermissionError"


@requires_linux_sandbox
def test_af_unix_and_ordinary_reads_still_work(enforced):
    assert enforced["unix"] == "allowed"
    assert enforced["read_etc"] == "allowed"
    assert enforced["read_proc"] == "allowed"
    # Landlock is an allowlist: if the interpreter's roots were wrong, importing
    # anything not already loaded would fail. This is the regression guard for
    # a missing read root.
    assert enforced["import"] == "allowed"


@requires_linux_sandbox
def test_writes_are_confined_to_the_workspace(enforced):
    assert enforced["write_in"] == "allowed"
    assert enforced["write_out"] == "PermissionError"


@requires_linux_sandbox
def test_home_read_jail(enforced):
    assert enforced["read_home"] == "PermissionError"


@requires_linux_sandbox
def test_the_unconfined_parent_cannot_be_inspected(enforced):
    # The parent holds ANTHROPIC_API_KEY and the vault passphrase and runs as the
    # same uid, so /proc/<parent>/environ would otherwise be readable. Landlock
    # hooks ptrace_access_check, and a restricted process may not inspect a
    # less-restricted one — which also closes PTRACE_ATTACH against the parent.
    assert enforced["parent_environ"] == "PermissionError"


@requires_linux_sandbox
def test_no_escape_by_exec(enforced):
    assert enforced["exec_tcp"] == "blocked"


@requires_linux_sandbox
def test_landlock_floor_is_enforced_not_assumed(monkeypatch):
    # Below the floor the backend must report unsupported so the launch gate
    # refuses, rather than installing a jail with an ungoverned truncate(2).
    import pyharness.broker.remote.linux_sandbox as ls

    monkeypatch.setattr(ls, "landlock_abi", lambda: MIN_LANDLOCK_ABI - 1)
    assert not ls.linux_sandbox_supported()
    with pytest.raises(OSError, match="landlock ABI"):
        ls.apply_landlock(None, ())


def test_cleanup_home_probe():
    probe = Path.home() / ".pyharness_probe"
    if probe.exists():
        probe.unlink()
