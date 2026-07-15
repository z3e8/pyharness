from __future__ import annotations

import subprocess

from ...core.workspace import Workspace
from ...security.env import minimal_environ


class ShellCapability:
    name = "shell"

    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def exports(self) -> dict:
        return {"bash": self.bash}

    def bash(self, cmd: str, timeout: int = 60) -> str:
        # The command runs parent-side (out-of-process) or in-process; either
        # way it must not inherit the parent's environment — a secret the parent
        # legitimately holds, or any var the user put in .env, would be one
        # `printenv` away. It gets the minimal allowlist environment instead
        # (see security/env.py; PYHARNESS_ENV_PASSTHROUGH admits extras). Output
        # comes back whole into a kernel variable; the display cap the agent sees
        # is the kernel's print guardrail, not this call. Redirect to a file for
        # a body too large to hold if needed.
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=self.ws.dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=minimal_environ(),
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            return output + f"\n[timed out after {timeout}s]"
        return proc.stdout + proc.stderr
