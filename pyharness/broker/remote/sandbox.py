from __future__ import annotations

import os
import sys
from pathlib import Path

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
# Two complementary layers, both best-effort and degrading silently where a
# platform can't honor them:
#   1. A macOS Seatbelt profile (`sandbox-exec`) enforcing three things: no
#      outbound network, no filesystem write outside the workspace, and a read
#      jail that hides the user's $HOME (re-allowing only what the interpreter
#      needs to run and import). Any process the child execs inherits the profile.
#   2. POSIX resource limits applied inside the child to bound blast radius
#      (no core dumps; on Linux, a cap on processes blunts fork bombs).
#
# Linux/container confinement (seccomp, namespaces) is not built here; on
# non-macOS only the resource limits apply. See docs/explanation/security-and-audit.md.

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def macos_sandbox_supported() -> bool:
    return sys.platform == "darwin" and os.path.exists(_SANDBOX_EXEC)


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


def make_child_executable(sbdir: Path, workspace: Path | None = None) -> str | None:
    """Return a launcher to use as the spawn executable, or None if this platform
    has no OS sandbox. The launcher runs the real interpreter under `sandbox-exec`
    with our profile; multiprocessing's spawn args (`$@`) and the inherited IPC
    pipe pass straight through. `sbdir` holds the generated profile and launcher;
    `workspace`, when given, scopes the write allowance and read jail to it."""
    if not macos_sandbox_supported():
        return None
    profile = sbdir / "child.sb"
    profile.write_text(_seatbelt_profile(workspace))
    launcher = sbdir / "sandboxed-python"
    launcher.write_text(
        "#!/bin/sh\n"
        f'exec {_SANDBOX_EXEC} -f "{profile}" "{sys.executable}" "$@"\n'
    )
    launcher.chmod(0o755)
    return str(launcher)


def _secret_env_keys(prefixes: tuple[str, ...], names: tuple[str, ...]) -> list[str]:
    blocked = set(names)
    return [
        key
        for key in os.environ
        if key in blocked or any(key.startswith(p) for p in prefixes)
    ]


def scrub_secret_env(prefixes: tuple[str, ...], names: tuple[str, ...]) -> None:
    """Delete secret-bearing variables from *this* process's environment.

    Called in the child before any agent code runs, so neither `os.environ` nor a
    subprocess the child spawns can read an env-backed secret or the file-vault
    passphrase. The parent keeps its own environment intact (the vault resolves
    secrets there); only the child's copy is stripped."""
    for key in _secret_env_keys(prefixes, names):
        os.environ.pop(key, None)


def scrubbed_environ(prefixes: tuple[str, ...], names: tuple[str, ...]) -> dict[str, str]:
    """A copy of the environment with secret-bearing variables removed, for a
    subprocess run on the agent's behalf (e.g. shell.bash) — so a command like
    `printenv` cannot read a vault secret the parent legitimately holds."""
    blocked = set(_secret_env_keys(prefixes, names))
    return {key: value for key, value in os.environ.items() if key not in blocked}


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
