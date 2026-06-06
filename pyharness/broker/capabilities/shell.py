from __future__ import annotations

import subprocess

from ...core.workspace import Workspace
from ...util import truncate


class ShellCapability:
    name = "shell"

    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def exports(self) -> dict:
        return {"bash": self.bash}

    def bash(self, cmd: str, timeout: int = 60) -> str:
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=self.ws.dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            return truncate(output + f"\n[timed out after {timeout}s]")
        return truncate(proc.stdout + proc.stderr)
