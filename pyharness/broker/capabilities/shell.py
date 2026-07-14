from __future__ import annotations

import subprocess

from ...core.workspace import Workspace
from ...security.vault import DEFAULT_ENV_PREFIX, PASSPHRASE_ENV
from ..remote.sandbox import scrubbed_environ


class ShellCapability:
    name = "shell"

    def __init__(
        self,
        workspace: Workspace,
        *,
        secret_env_prefixes: tuple[str, ...] = (DEFAULT_ENV_PREFIX,),
        secret_env_names: tuple[str, ...] = (PASSPHRASE_ENV,),
    ):
        self.ws = workspace
        self._secret_env = (secret_env_prefixes, secret_env_names)

    def exports(self) -> dict:
        return {"bash": self.bash}

    def bash(self, cmd: str, timeout: int = 60) -> str:
        # The command runs parent-side (out-of-process) or in-process; either
        # way it must not inherit vault secrets the parent holds, so it gets an
        # environment with the secret-bearing variables stripped. Output comes
        # back whole into a kernel variable; the display cap the agent sees is the
        # kernel's print guardrail, not this call. Redirect to a file for a body
        # too large to hold if needed.
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=self.ws.dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=scrubbed_environ(*self._secret_env),
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            return output + f"\n[timed out after {timeout}s]"
        return proc.stdout + proc.stderr
