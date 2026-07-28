from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import sys
from pathlib import Path

# Linux OS-level confinement for the out-of-process child — the sibling of the
# macOS Seatbelt profile in sandbox.py, enforcing the same three invariants: no
# outbound network, no filesystem write outside the workspace, and a read jail
# that hides the user's $HOME.
#
# Two mechanisms, both applied by the process *to itself* (no helper binary, no
# user namespaces, no root), and both irrevocable and inherited across exec:
#
#   1. Landlock (Linux 5.13+) for the filesystem. A ruleset naming the readable
#      and writable subtrees; everything unnamed is denied.
#   2. A seccomp-bpf filter denying socket(2) for AF_INET/AF_INET6/AF_PACKET.
#      Landlock's own network support (ABI 4) is TCP-bind/connect-by-port only
#      and leaves UDP open, so it cannot express "no outbound network" — a
#      seccomp filter keyed on the address family covers TCP, UDP and raw in one
#      rule on any kernel since 3.5.
#
# Bubblewrap was the obvious alternative and was rejected: it needs unprivileged
# user namespaces, which Ubuntu 24.04 LTS blocks out of the box (AppArmor, no
# working bwrap profile until 25.04) and which Docker's default seccomp profile
# blocks inside containers. See agents/plan-linux-confinement.md.
#
# THE MODEL DIFFERENCE THAT MATTERS: Seatbelt is a denylist ((allow default) plus
# targeted denies), Landlock is an allowlist with no subtraction operator. So the
# macOS profile only enumerates what is forbidden, while this module must
# enumerate everything the child legitimately reads — including the per-session
# venv, whose site-packages lives outside the workspace under the host's sbdir.
# Omitting a root here does not weaken security; it breaks an import at runtime.

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2

_A_EXECUTE = 1 << 0
_A_READ_FILE = 1 << 2
_A_READ_DIR = 1 << 3
_A_REFER = 1 << 13  # ABI 2
_A_TRUNCATE = 1 << 14  # ABI 3

# The full set of access bits each ABI knows about. `handled_access_fs` must not
# name a bit the running kernel does not understand (EINVAL), and every bit we
# *fail* to handle is an operation left completely ungoverned — so this tracks
# the ABI exactly rather than picking a convenient subset.
_FS_HANDLED_BY_ABI = {
    1: (1 << 13) - 1,
    2: (1 << 14) - 1,
    3: (1 << 15) - 1,
    4: (1 << 15) - 1,  # ABI 4 adds network bits only
    5: (1 << 16) - 1,
}
_MAX_KNOWN_ABI = max(_FS_HANDLED_BY_ABI)

# ABI 3 (Linux 6.2) is the floor: it is the first ABI with LANDLOCK_ACCESS_FS_
# TRUNCATE, without which truncate(2) on an already-open descriptor escapes the
# write jail. Below it we report unsupported rather than ship a jail with a hole
# — the launch gate in sandbox.py then refuses to start, which is the honest
# outcome. Covers Ubuntu 24.04 LTS (6.8) and Debian 13 (6.12); excludes Ubuntu
# 22.04 (5.15) and Debian 12 (6.1).
MIN_LANDLOCK_ABI = 3

_READ_ACCESS = _A_READ_FILE | _A_READ_DIR | _A_EXECUTE

# System roots the interpreter and arbitrary agent-installed packages read at
# import time. $HOME is simply never granted, so the read jail falls out of the
# allowlist for free. /proc is granted whole because libraries routinely read
# /proc/meminfo, /proc/cpuinfo and friends; the sensitive per-process files
# (/proc/<pid>/environ, /proc/<pid>/maps) stay protected regardless, because
# they go through ptrace_may_access and Landlock hooks ptrace_access_check —
# a Landlock-restricted process cannot inspect a less-restricted one. That also
# closes PTRACE_ATTACH against the unsandboxed parent, which holds the API key
# and the vault passphrase.
_SYSTEM_READ_ROOTS = (
    "/usr",
    "/lib",
    "/lib64",
    "/bin",
    "/sbin",
    "/etc",
    "/opt",
    "/proc",
    "/dev",
)

# seccomp: audit arch constant and the socket(2) syscall number, per arch. An
# arch missing here reports unsupported — on 32-bit x86 in particular glibc
# routes through socketcall(2), which packs the address family behind a
# userspace pointer that classic BPF cannot dereference, so a family-keyed
# filter would silently under-enforce there.
_ARCH = {
    "x86_64": (0xC000003E, 41),
    "aarch64": (0xC00000B7, 198),
    "arm64": (0xC00000B7, 198),
}

# Writable regardless of the workspace, mirroring the macOS profile's
# `(allow file-write-data (literal "/dev/null") (regex #"^/dev/tty"))`: a discard
# sink and the human-visible diagnostic channel. Landlock rules are per-inode, so
# these are added as file rules rather than subtrees.
_WRITE_ALLOW_FILES = ("/dev/null", "/dev/tty")
_A_WRITE_FILE = 1 << 1

# O_PATH is Linux-only, so it is absent from the `os` module on macOS/Windows and
# referencing it directly fails type-checking there. Nothing in this module ever
# executes off Linux; the fallback is the kernel's own value, never used.
_O_PATH = getattr(os, "O_PATH", 0o010000000)

_AF_INET, _AF_INET6, _AF_PACKET = 2, 10, 17

_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000

# struct seccomp_data field offsets (linux/seccomp.h).
_OFF_NR = 0
_OFF_ARCH = 4
_OFF_ARG0 = 16  # low 32 bits of args[0] on a little-endian 64-bit arch

_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_RET_K = 0x06


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    # Packed in the kernel ABI: 8-byte access mask immediately followed by a
    # 4-byte fd, with no tail padding. ctypes would otherwise pad to 16 and the
    # kernel would reject the size.
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def landlock_abi() -> int:
    """The Landlock ABI version this kernel implements, or 0 if Landlock is
    absent/disabled. A probe call, no side effects."""
    if sys.platform != "linux":
        return 0
    try:
        rc = _libc().syscall(
            _SYS_LANDLOCK_CREATE_RULESET, None, 0, _LANDLOCK_CREATE_RULESET_VERSION
        )
    except OSError:
        return 0
    return rc if rc > 0 else 0


def _arch_entry() -> tuple[int, int] | None:
    return _ARCH.get(platform.machine().lower())


def linux_sandbox_supported() -> bool:
    """Whether both halves of the Linux backend can be enforced here. Both are
    required: a filesystem jail with no network denial (or the reverse) is a
    partial perimeter, and the launch gate's contract is all-or-nothing."""
    return (
        sys.platform == "linux"
        and landlock_abi() >= MIN_LANDLOCK_ABI
        and _arch_entry() is not None
        and os.path.exists("/proc/sys/kernel/seccomp/actions_avail")
    )


def _read_roots(workspace: Path | None, extra: tuple[str, ...]) -> list[str]:
    """Everything the child may read: the system roots, the interpreter (venv and
    the managed CPython), our own package source, the workspace, and any caller-
    supplied extras (the per-session venv's site-packages, which lives outside
    the workspace under the host's sbdir)."""
    roots = list(_SYSTEM_READ_ROOTS)
    roots.append(str(Path(sys.prefix).resolve()))
    roots.append(str(Path(sys.base_prefix).resolve()))
    roots.append(str(Path(__file__).resolve().parents[2]))
    if workspace is not None:
        roots.append(str(workspace.resolve()))
    roots.extend(extra)
    return roots


def apply_landlock(
    workspace: Path | None, extra_read_roots: tuple[str, ...] = ()
) -> None:
    """Install the filesystem jail: read the roots above, write only inside the
    workspace. Irrevocable and inherited across exec, so anything the child
    subsequently execs is confined too."""
    abi = landlock_abi()
    if abi < MIN_LANDLOCK_ABI:
        raise OSError(f"landlock ABI {abi} < required {MIN_LANDLOCK_ABI}")
    libc = _libc()
    handled = _FS_HANDLED_BY_ABI[min(abi, _MAX_KNOWN_ABI)]

    attr = _RulesetAttr(handled_access_fs=handled, handled_access_net=0)
    # ABI < 4 has no handled_access_net field; passing the longer struct is an
    # EINVAL there, so the declared size tracks the ABI too.
    size = ctypes.sizeof(attr) if abi >= 4 else 8
    ruleset = libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), size, 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")

    try:
        writable = {str(workspace.resolve())} if workspace is not None else set()
        for path in _WRITE_ALLOW_FILES:
            try:
                fd = os.open(path, _O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # no controlling tty is normal in a headless run
            try:
                rule = _PathBeneathAttr(allowed_access=_A_WRITE_FILE, parent_fd=fd)
                if (
                    libc.syscall(
                        _SYS_LANDLOCK_ADD_RULE,
                        ruleset,
                        _LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(rule),
                        0,
                    )
                    < 0
                ):
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule({path})")
            finally:
                os.close(fd)
        for path in _read_roots(workspace, extra_read_roots):
            # REFER (link/rename across directories) is only meaningful on a
            # writable tree and is rejected on a read-only rule, so the read mask
            # never carries it.
            access = handled if path in writable else _READ_ACCESS
            try:
                fd = os.open(path, _O_PATH | os.O_CLOEXEC)
            except OSError:
                # A root that does not exist on this distro (/lib64 on arm64, an
                # absent /opt) is skipped: it grants nothing and denies nothing.
                continue
            try:
                rule = _PathBeneathAttr(allowed_access=access, parent_fd=fd)
                rc = libc.syscall(
                    _SYS_LANDLOCK_ADD_RULE,
                    ruleset,
                    _LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.byref(rule),
                    0,
                )
                if rc < 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule({path})")
            finally:
                os.close(fd)

        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
        if libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(ruleset)


def _network_deny_filter(audit_arch: int, nr_socket: int) -> list[_SockFilter]:
    """Classic-BPF program: EPERM any socket(2) whose address family reaches off
    the box, allow everything else.

    The arch check on line 0 is load-bearing, not a formality: without it a
    process could enter via a different ABI (x32 on x86_64) where the same
    syscall *number* means something else, and the filter would be reading the
    wrong syscall's arguments. Mismatch kills the process rather than falling
    through, because a filter that cannot identify the ABI cannot make a safe
    decision."""
    deny = 11
    return [
        _SockFilter(_BPF_LD_W_ABS, 0, 0, _OFF_ARCH),
        _SockFilter(_BPF_JEQ_K, 1, 0, audit_arch),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        _SockFilter(_BPF_LD_W_ABS, 0, 0, _OFF_NR),
        _SockFilter(_BPF_JEQ_K, 1, 0, nr_socket),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
        _SockFilter(_BPF_LD_W_ABS, 0, 0, _OFF_ARG0),
        _SockFilter(_BPF_JEQ_K, deny - 8, 0, _AF_INET),
        _SockFilter(_BPF_JEQ_K, deny - 9, 0, _AF_INET6),
        _SockFilter(_BPF_JEQ_K, deny - 10, 0, _AF_PACKET),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | 1),  # EPERM
    ]


def apply_seccomp_network_deny() -> None:
    """Install the network filter. AF_UNIX and AF_NETLINK are deliberately left
    alone — neither reaches off the box, and denying them breaks ordinary library
    behaviour; the filesystem side of AF_UNIX is governed by Landlock."""
    entry = _arch_entry()
    if entry is None:
        raise OSError(f"no seccomp arch profile for {platform.machine()!r}")
    libc = _libc()
    program = _network_deny_filter(*entry)
    buf = (_SockFilter * len(program))(*program)
    fprog = _SockFprog(len=len(program), filter=buf)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) < 0:
        raise OSError(ctypes.get_errno(), "PR_SET_SECCOMP")


def apply_linux_sandbox(
    workspace: Path | None, extra_read_roots: tuple[str, ...] = ()
) -> None:
    """Apply both halves. Network first: if the filesystem jail were installed
    first and the seccomp step then failed, the process would be left confined
    but still able to reach the network — the more dangerous half-state."""
    apply_seccomp_network_deny()
    apply_landlock(workspace, extra_read_roots)
