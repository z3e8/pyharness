from __future__ import annotations

import os
import sys
from pathlib import Path

# OS-level confinement for the out-of-process child (design §3 / §11).
#
# The child is the agent's userland: it runs the LLM-authored Python. Every
# legitimate side effect goes through the broker in the (unsandboxed) parent
# over IPC, so the child itself needs neither the network nor write access to
# the real filesystem — only the ability to read its libraries and compute. The
# process boundary alone does not enforce that (the child can still `import os`);
# this module adds the OS enforcement the design defers to "later".
#
# Two complementary layers, both best-effort and degrading silently where a
# platform can't honor them:
#   1. A macOS Seatbelt profile (`sandbox-exec`) that denies exactly the two
#      channels that would bypass the broker: outbound network and filesystem
#      writes. Any process the child execs inherits the profile, so shelling out
#      cannot escape these limits either.
#   2. POSIX resource limits applied inside the child to bound blast radius
#      (no core dumps; a cap on processes blunts fork bombs).
#
# Linux/container confinement (seccomp, namespaces) is not built here; on
# non-macOS only the resource limits apply. See docs/explanation/security-and-audit.md.

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Seatbelt profile. We allow by default (the interpreter must read its shared
# libraries and the agent may compute freely) and deny the two side-effect
# channels: outbound network and *all* filesystem writes. The child needs no
# disk write to function — spawn/IPC is over pipes, and the .pyc cache write
# that imports attempt is denied harmlessly (imports fall back to in-memory
# compilation). The only write-allowance is the null sink and the tty, which is
# a human-visible diagnostic channel, not hidden persistence. Anything else the
# child wrote would be invisible to both the agent and the human and would
# bypass the broker — exactly what this sandbox exists to prevent.
_PROFILE = r"""(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write-data
  (literal "/dev/null")
  (regex #"^/dev/tty"))
"""


def macos_sandbox_supported() -> bool:
    return sys.platform == "darwin" and os.path.exists(_SANDBOX_EXEC)


def make_child_executable(workdir: Path) -> str | None:
    """Return a launcher to use as the spawn executable, or None if this
    platform has no OS sandbox. The launcher runs the real interpreter under
    `sandbox-exec` with our profile; multiprocessing's spawn args (`$@`) and the
    inherited IPC pipe pass straight through."""
    if not macos_sandbox_supported():
        return None
    profile = workdir / "child.sb"
    profile.write_text(_PROFILE)
    launcher = workdir / "sandboxed-python"
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
    if hasattr(resource, "RLIMIT_NPROC"):
        limits.append((resource.RLIMIT_NPROC, 512))  # blunt fork bombs
    for what, soft in limits:
        try:
            _, hard = resource.getrlimit(what)
            capped = soft if hard == resource.RLIM_INFINITY else min(soft, hard)
            resource.setrlimit(what, (capped, hard))
        except (ValueError, OSError):
            pass
