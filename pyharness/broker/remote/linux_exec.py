from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .linux_sandbox import apply_linux_sandbox

# Launcher for `shell.bash` on Linux — the counterpart of the generated
# `sandboxed-python` shell script the macOS path writes.
#
# `bash` executes in the privileged *parent*, so unlike the out-of-process child
# it cannot confine itself: by the time the command is chosen, the process that
# would apply the restrictions is the broker. The confinement therefore has to
# land between fork and exec, and the only two ways to do that are
# `subprocess(preexec_fn=…)` and a launcher that re-execs. `preexec_fn` is out —
# the broker parent is multi-threaded, and arbitrary work after `fork()` in a
# threaded process can deadlock on an allocator lock held by a thread that no
# longer exists. So: a launcher process applies the sandbox to itself and then
# `execv`s the real command, inheriting the restrictions (both Landlock and
# seccomp survive exec).
#
# Invoked as:
#   python -m pyharness.broker.remote.linux_exec '<json spec>' /bin/sh -c '<cmd>'
#
# Costs one CPython startup (~30-50ms) per bash() call, where macOS pays only
# `/bin/sh` + `sandbox-exec`. Acceptable for an approval-gated capability.


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: linux_exec <json-spec> <program> [args...]", file=sys.stderr)
        return 2
    spec = json.loads(argv[1])
    workspace = Path(spec["workspace"]) if spec.get("workspace") else None
    apply_linux_sandbox(
        workspace,
        tuple(spec.get("read_roots", ())),
        tuple(spec.get("write_roots", ())),
        # Only the package-install path sets this: pip must reach the index, so
        # the seccomp half is dropped while the filesystem jail stays in force.
        deny_network=not spec.get("allow_network", False),
    )
    program = argv[2]
    os.execv(program, argv[2:])
    return 127  # unreachable: execv only returns by raising


if __name__ == "__main__":
    sys.exit(main(sys.argv))
