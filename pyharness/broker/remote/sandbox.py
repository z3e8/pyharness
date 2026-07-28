from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .linux_sandbox import MIN_LANDLOCK_ABI, linux_sandbox_supported

# OS-level confinement for the out-of-process child (design §3 / §11).
#
# The child is the agent's userland: it runs the LLM-authored Python. The guiding
# rule is "the workspace is the sandbox; the broker guards the perimeter." Agent
# code may read and write freely *inside* its session workspace (so libraries that
# persist files just work), but everything that leaves the box — the network,
# writes anywhere else, reads of the user's personal files — is denied at the OS
# level. Every legitimate outward side effect goes through the broker in the
# (unsandboxed) parent over IPC instead.
#
# Two complementary layers:
#   1. A macOS Seatbelt profile (`sandbox-exec`) enforcing three things: no
#      outbound network, no filesystem write outside the workspace, and a read
#      jail that hides the user's $HOME (re-allowing only what the interpreter
#      needs to run and import). Any process the child execs inherits the profile.
#   2. POSIX resource limits applied inside the child to bound blast radius
#      (no core dumps; on Linux, a cap on processes blunts fork bombs).
#
# Linux enforces the same three invariants through a different mechanism —
# Landlock for the filesystem, a seccomp-bpf filter for the network — in
# `linux_sandbox.py`. The two backends differ in *shape*, not in what they
# promise: macOS wraps the child's exec in a declarative profile, while Linux has
# the child restrict itself at startup (and uses a re-exec launcher for the one
# case, `shell.bash`, that runs parent-side and so cannot self-restrict).
#
# Windows has no confinement story and loses the rlimits too. That absence is
# loud, not silent: `check_unsandboxed_platform` refuses to start a kernel on a
# platform with no sandbox unless PYHARNESS_ALLOW_UNSANDBOXED opts in
# explicitly, and warns on stderr when it does. See
# docs/explanation/security-and-audit.md.

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

ALLOW_UNSANDBOXED_ENV = "PYHARNESS_ALLOW_UNSANDBOXED"


def macos_sandbox_supported() -> bool:
    return sys.platform == "darwin" and os.path.exists(_SANDBOX_EXEC)


def os_sandbox_supported() -> bool:
    """Whether this platform can confine agent code at the OS level — the single
    source of truth the launch gate consults. Two backends today: macOS Seatbelt
    and Linux Landlock+seccomp. The predicate stays all-or-nothing on purpose; a
    platform that can enforce only some of the three invariants reports False and
    the gate refuses, rather than the harness claiming a partial perimeter."""
    return macos_sandbox_supported() or linux_sandbox_supported()


class UnsandboxedPlatformError(RuntimeError):
    """No OS sandbox exists on this platform and the explicit opt-in
    (PYHARNESS_ALLOW_UNSANDBOXED) is not set."""


_UNSANDBOXED_WARNED = False


def check_unsandboxed_platform() -> None:
    """The loud opt-in gate for platforms with no OS sandbox (today: Windows, and
    Linux below the Landlock floor). There, agent code would run with the full
    filesystem and network reach — only the process boundary, the minimal
    environment, and (POSIX) rlimits remain — so by default pyharness refuses to
    start a kernel rather than run LLM-authored code unconfined. Setting
    PYHARNESS_ALLOW_UNSANDBOXED=true proceeds anyway, with a one-time stderr
    warning. Where a sandbox is available this is a no-op."""
    global _UNSANDBOXED_WARNED
    if os_sandbox_supported():
        return
    if os.environ.get(ALLOW_UNSANDBOXED_ENV, "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        windows = (
            " (and on Windows, no resource limits either)"
            if sys.platform == "win32"
            else ""
        )
        if sys.platform == "linux":
            why = (
                " (Linux confinement needs Landlock ABI "
                f"{MIN_LANDLOCK_ABI}+, i.e. kernel 6.2 or newer, on x86_64 or "
                "aarch64)"
            )
        else:
            why = " (confinement is built for macOS and Linux only)"
        raise UnsandboxedPlatformError(
            "no OS sandbox is available on this platform"
            f"{why}, so agent-authored code would run with your full user "
            "privileges: unrestricted filesystem and network "
            f"access{windows}. Refusing to start. Set {ALLOW_UNSANDBOXED_ENV}=true "
            "to run anyway (a loud warning is printed). "
            "See docs/explanation/security-and-audit.md."
        )
    if not _UNSANDBOXED_WARNED:
        _UNSANDBOXED_WARNED = True
        print(
            f"WARNING: {ALLOW_UNSANDBOXED_ENV} is set and this platform has no OS "
            "sandbox — agent code runs UNCONFINED (full filesystem and network "
            "access as your user). Only the minimal environment and POSIX rlimits "
            "still apply.",
            file=sys.stderr,
        )


def _sbpl_quote(path: str) -> str:
    """Quote a path as an SBPL string literal."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _package_dir() -> Path:
    """Our own package source (`<repo>/pyharness` editable, or `site-packages/
    pyharness` in a wheel). The child imports these modules to boot, so the read
    jail must keep the subtree readable."""
    return Path(__file__).resolve().parents[2]


def _sys_path_entry() -> Path:
    """The sys.path directory that resolves our package — the editable repo root,
    or the site-packages dir in a wheel. Python must *list* this directory to
    import pyharness, but nothing else under it (a repo's .env, other sessions,
    unrelated source) needs to be readable, so it is allow-listed for the directory
    entry only, not as a subtree."""
    return _package_dir().parent


def _read_allow_roots(workspace: Path) -> list[str]:
    """Subtrees the child reads in full from inside the $HOME jail: its workspace,
    the interpreter (the venv and the managed CPython), and our own package source.
    Anything outside $HOME (system libraries, the temp sandbox dir) stays readable
    via `allow default`, so only these HOME-resident paths need re-allowing."""
    roots = {
        workspace.resolve(),
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        _package_dir(),
    }
    return sorted(str(p) for p in roots)


def _seatbelt_profile(workspace: Path | None) -> str:
    """Build the Seatbelt profile. Two invariants always hold: no outbound network,
    and no filesystem write outside the workspace. When a workspace is known, the
    child also gets ergonomic in-workspace writes and a read jail over $HOME."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        '(allow file-write-data (literal "/dev/null") (regex #"^/dev/tty"))',
    ]
    if workspace is not None:
        ws = workspace.resolve()
        # Ergonomic writes: read and write freely *inside* the workspace, so
        # libraries that persist files (savefig, to_csv, ...) work unchanged. Every
        # other write stays denied by the rule above.
        lines.append(f"(allow file-write* (subpath {_sbpl_quote(str(ws))}))")
        # Read jail: deny the *contents* of the user's personal files ($HOME), then
        # re-allow only what the interpreter needs to run and import. We deny
        # `file-read-data`, not `file-read*`: the latter also covers read-metadata,
        # which the loader needs to exec the interpreter, so denying it wholesale
        # breaks process startup (EPERM). Denying data alone hides file contents
        # while leaving stat/exec intact. Last matching rule wins, so the narrower
        # allows below override the $HOME deny.
        lines.append(f"(deny file-read-data (subpath {_sbpl_quote(str(Path.home()))}))")
        lines.append("(allow file-read-data")
        for root in _read_allow_roots(ws):
            lines.append(f"  (subpath {_sbpl_quote(root)})")
        # The sys.path entry that resolves our package must be *listable* so Python
        # can find pyharness, but its other contents (a repo's .env, prior sessions,
        # unrelated source) stay unreadable: allow the directory entry itself, not a
        # subtree. In a wheel install this dir sits under sys.prefix already; the
        # extra literal is harmless.
        lines.append(f"  (literal {_sbpl_quote(str(_sys_path_entry()))})")
        lines.append(")")
    return "\n".join(lines) + "\n"


def _install_profile(write_roots: list[Path]) -> str:
    """Profile for a package install. It differs from the child profile in the
    one way it has to: **outbound network is allowed**, because pip must reach
    the index. Everything else is the point of wrapping it at all — a package's
    `setup.py` or build hook is arbitrary code running at install time, and here
    it cannot read `$HOME` (`~/.ssh`, `~/.aws`, a project `.env`) or write
    anywhere except the roots below.

    `write_roots` are the venv and a dedicated scratch dir, deliberately *not*
    the whole sandbox dir or the whole tempdir: the generated Seatbelt profiles
    live directly in the sandbox dir, so a build hook that could write there
    could rewrite the jail that confines the next child."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        '(allow file-write-data (literal "/dev/null") (regex #"^/dev/tty"))',
    ]
    for root in write_roots:
        lines.append(
            f"(allow file-write* (subpath {_sbpl_quote(str(root.resolve()))}))"
        )
    lines.append(f"(deny file-read-data (subpath {_sbpl_quote(str(Path.home()))}))")
    lines.append("(allow file-read-data")
    roots = {str(r.resolve()) for r in write_roots}
    roots |= {
        str(Path(sys.prefix).resolve()),
        str(Path(sys.base_prefix).resolve()),
        str(_package_dir()),
    }
    for root in sorted(roots):
        lines.append(f"  (subpath {_sbpl_quote(root)})")
    lines.append(f"  (literal {_sbpl_quote(str(_sys_path_entry()))})")
    lines.append(")")
    return "\n".join(lines) + "\n"


def sandboxed_install_argv(
    argv: list[str], sbdir: Path, write_roots: list[Path]
) -> list[str] | None:
    """Wrap a package-install command so its build hooks run confined, or None
    where no OS sandbox exists. `packages.install` runs in the privileged parent
    like `shell.bash`, but unlike it needs the network, so this uses the
    install profile above rather than the child's."""
    if macos_sandbox_supported():
        profile = sbdir / "install.sb"
        profile.write_text(_install_profile(write_roots))
        return [_SANDBOX_EXEC, "-f", str(profile), *argv]
    if linux_sandbox_supported():
        spec = json.dumps(
            {
                "workspace": None,
                "read_roots": [str(r) for r in write_roots],
                "write_roots": [str(r) for r in write_roots],
                "allow_network": True,
            }
        )
        return [sys.executable, "-m", "pyharness.broker.remote.linux_exec", spec, *argv]
    return None


def make_child_executable(sbdir: Path, workspace: Path | None = None) -> str | None:
    """Return a launcher to use as the spawn executable, or None if this platform
    needs no launcher. The launcher runs the real interpreter under `sandbox-exec`
    with our profile; multiprocessing's spawn args (`$@`) and the inherited IPC
    pipe pass straight through. `sbdir` holds the generated profile and launcher;
    `workspace`, when given, scopes the write allowance and read jail to it.

    Linux deliberately returns None: there the child confines *itself* in
    `child_main` (see `linux_sandbox.apply_linux_sandbox`), which avoids putting
    a second exec between multiprocessing and its inherited pipe fd. The window
    before that call runs only our own bootstrap, never agent code."""
    if not macos_sandbox_supported():
        return None
    profile = sbdir / "child.sb"
    profile.write_text(_seatbelt_profile(workspace))
    launcher = sbdir / "sandboxed-python"
    launcher.write_text(
        f'#!/bin/sh\nexec {_SANDBOX_EXEC} -f "{profile}" "{sys.executable}" "$@"\n'
    )
    launcher.chmod(0o755)
    return str(launcher)


def sandboxed_shell_argv(
    cmd: str, sbdir: Path, workspace: Path | None = None
) -> list[str] | None:
    """Return an argv that runs `cmd` under `/bin/sh -c` inside the same
    Seatbelt profile the out-of-process child gets — no outbound network, writes
    confined to the workspace, the $HOME read jail — or None when this platform
    has no OS sandbox. `shell.bash` executes in the privileged parent, so
    without this wrapper the command would run with the parent's full OS reach.
    `sbdir` holds the generated profile; it must sit *outside* the workspace so
    a sandboxed command cannot rewrite its own jail. A None return means the
    caller falls back to a plain `shell=True` run where env scrubbing is the
    only containment.

    On Linux the same job is done by a re-exec launcher (`linux_exec`) rather
    than a profile file, because `bash` runs parent-side and so cannot restrict
    itself; the returned argv shape is identical either way, so callers need no
    platform branch."""
    if macos_sandbox_supported():
        profile = sbdir / "shell.sb"
        profile.write_text(_seatbelt_profile(workspace))
        return [_SANDBOX_EXEC, "-f", str(profile), "/bin/sh", "-c", cmd]
    if linux_sandbox_supported():
        # The session venv lives at `sbdir/venv` (host.py), outside the
        # workspace, so a command that runs an installed tool needs it readable.
        spec = json.dumps(
            {
                "workspace": str(workspace) if workspace is not None else None,
                "read_roots": [str(sbdir)],
            }
        )
        return [
            sys.executable,
            "-m",
            "pyharness.broker.remote.linux_exec",
            spec,
            "/bin/sh",
            "-c",
            cmd,
        ]
    return None


def apply_resource_limits() -> None:
    """Bound the child's blast radius with POSIX rlimits. Each is guarded: a
    platform (or value) the kernel rejects is simply skipped, never fatal."""
    try:
        import resource
    except ImportError:
        return
    limits = [(resource.RLIMIT_CORE, 0)]  # no core dumps (could leak memory to disk)
    # RLIMIT_NPROC is per *user* on macOS/BSD, not per process tree, so a modest cap
    # counts every process the user already runs and makes ordinary fork()s
    # (subprocess, multiprocessing) fail with EAGAIN. Only apply it where it bounds
    # this process's descendants (Linux); skip it on Darwin.
    if hasattr(resource, "RLIMIT_NPROC") and sys.platform != "darwin":
        limits.append((resource.RLIMIT_NPROC, 512))  # blunt fork bombs
    for what, soft in limits:
        try:
            _, hard = resource.getrlimit(what)
            capped = soft if hard == resource.RLIM_INFINITY else min(soft, hard)
            resource.setrlimit(what, (capped, hard))
        except (ValueError, OSError):
            pass
