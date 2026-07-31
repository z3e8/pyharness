"""Prove the OS sandbox engages inside this container — or fail loudly.

The single worst outcome for a containment project is a container that quietly
runs agent code unconfined while the docs claim otherwise. This script makes
that outcome impossible to miss: it starts a *real* sandboxed kernel through
the same code path a session uses (`RemoteKernel` -> multiprocessing spawn ->
the child restricting itself with Landlock + seccomp), then probes enforcement
from inside agent code. It needs no API key and no network.

Run it in the built image (`make docker-verify` does exactly this):

    docker run --rm --entrypoint python pyharness /opt/pyharness/verify-sandbox.py

Exit codes:
    0  the sandbox engaged and every probe was enforced
    1  the sandbox claims support but a probe was NOT enforced — report this
    2  no sandbox is available here (pyharness would refuse to start a kernel)

An exit of 2 is the fail-closed path working as designed: the fix is a Docker
host whose kernel offers Landlock ABI >= 3 (Linux 6.2+ with the landlock LSM
enabled), never PYHARNESS_ALLOW_UNSANDBOXED.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

# What the sandboxed child must and must not be able to do. Each probe runs as
# agent code inside the kernel; the cell prints a dict of outcomes which the
# parent parses and judges against this table. "PermissionError" is the only
# outcome that counts as enforcement — a DNS error or a missing file must never
# be credited to the sandbox.
EXPECT = {
    "tcp": "PermissionError",  # socket(AF_INET) — seccomp, not the broker
    "udp": "PermissionError",  # socket(AF_INET, DGRAM) — Landlock ABI 4 can't do this
    "tcp6": "PermissionError",  # socket(AF_INET6)
    "write_outside": "PermissionError",  # Landlock write jail
    "read_home": "PermissionError",  # Landlock read jail over $HOME
    "write_inside": "allowed",  # the workspace stays fully usable
    "read_etc": "allowed",  # system config stays readable (interpreter needs it)
    "exec_escape": "blocked",  # a subprocess inherits both restrictions
}

PROBE_CELL = """
import socket, subprocess, sys
from pathlib import Path
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
probe("write_outside", lambda: Path("/tmp/pyharness-escape").write_text("x"))
probe("read_home", lambda: Path({home_probe!r}).read_text())
probe("write_inside", lambda: (Path({ws!r}) / "ok.txt").write_text("ok"))
probe("read_etc", lambda: Path("/etc/hostname").read_text())
r = subprocess.run([sys.executable, "-c",
    "import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM); print('ESCAPED')"],
    capture_output=True, text=True)
results["exec_escape"] = "allowed" if "ESCAPED" in r.stdout else "blocked"
print("PROBES=" + repr(results))
"""


def main() -> int:
    uname = os.uname()
    print(f"platform : {sys.platform} {uname.machine}, kernel {uname.release}")

    if sys.platform != "linux":
        print("FAIL: this check verifies the Linux container; run it under Docker.")
        return 2

    from pyharness.broker.remote.linux_sandbox import (
        MIN_LANDLOCK_ABI,
        landlock_abi,
        linux_sandbox_supported,
    )

    abi = landlock_abi()
    print(f"landlock : ABI {abi} (floor {MIN_LANDLOCK_ABI})")
    print(f"seccomp  : {os.path.exists('/proc/sys/kernel/seccomp/actions_avail')}")

    if not linux_sandbox_supported():
        print(
            "\nNO SANDBOX AVAILABLE HERE. pyharness fails closed: it will refuse\n"
            "to start a kernel in this container rather than run agent code\n"
            "unconfined. Fix the host — a kernel with Landlock ABI "
            f">= {MIN_LANDLOCK_ABI}\n"
            "(Linux 6.2+, landlock in the LSM list) on x86_64/aarch64 — and do\n"
            "NOT set PYHARNESS_ALLOW_UNSANDBOXED to paper over this."
        )
        return 2

    # The real path: a RemoteKernel over a minimal broker, exactly as a session
    # builds one (spawned child, self-applied Landlock + seccomp, workspace as
    # the only writable subtree).
    from pyharness import Budget, Policy, Workspace
    from pyharness.audit import AuditLog
    from pyharness.broker import Broker
    from pyharness.broker.remote import RemoteKernel

    tmp = Path(tempfile.mkdtemp(prefix="pyharness-verify-"))
    home_probe = Path.home() / ".pyharness_verify_secret"
    home_probe.write_text("SECRET")
    workspace = Workspace(tmp / "session")
    broker = Broker(Policy(), AuditLog(tmp / "audit.jsonl"), Budget())
    kernel = RemoteKernel(broker, workspace=workspace)
    try:
        out = kernel.run(
            PROBE_CELL.format(home_probe=str(home_probe), ws=str(workspace.dir))
        )
    finally:
        kernel.close()
        home_probe.unlink(missing_ok=True)

    marker = next(
        (line for line in out.splitlines() if line.startswith("PROBES=")), None
    )
    if marker is None:
        print(f"FAIL: probe cell produced no verdict. Kernel output:\n{out}")
        return 1
    results = ast.literal_eval(marker.removeprefix("PROBES="))

    failed = []
    for name, want in EXPECT.items():
        got = results.get(name, "<missing>")
        status = "ok" if got == want else "FAIL"
        print(f"{status:4} {name:14} expected {want}, got {got}")
        if got != want:
            failed.append(name)

    if failed:
        print(
            f"\nFAIL: {len(failed)} probe(s) not enforced ({', '.join(failed)}).\n"
            "The sandbox reported support but did not confine agent code —\n"
            "treat this container as UNCONFINED and report it."
        )
        return 1
    print("\nok: Landlock + seccomp engaged inside the container; all probes enforced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
