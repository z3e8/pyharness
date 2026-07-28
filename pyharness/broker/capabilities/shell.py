from __future__ import annotations

import subprocess

from ...core.workspace import Workspace
from ...security.env import minimal_environ
from ...security.policy import ActionCategory
from ...util import truncate
from ..remote.sandbox import sandboxed_shell_argv


class ShellCapability:
    name = "shell"

    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def exports(self) -> dict:
        return {"bash": self.bash}

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """An arbitrary program is an arbitrary program — the OS sandbox confines
        fs/network but not what the command *does*, and on an unsandboxed platform
        there is no jail at all — so class it OUTWARD and show the human the
        command line itself."""
        cmd = kwargs.get("cmd") or (args[0] if args else "?")
        return ActionCategory.OUTWARD, f"bash: {truncate(str(cmd), 300)}"

    def bash(self, cmd: str, timeout: int = 60) -> str:
        # The command runs parent-side (out-of-process) or in-process; either
        # way it must not inherit the parent's environment — a secret the parent
        # legitimately holds, or any var the user put in .env, would be one
        # `printenv` away. It gets the minimal allowlist environment instead
        # (see security/env.py; PYHARNESS_ENV_PASSTHROUGH admits extras). Output
        # comes back whole into a kernel variable; the display cap the agent sees
        # is the kernel's print guardrail, not this call. Redirect to a file for
        # a body too large to hold if needed.
        #
        # Parent-side must not mean parent-privileged: on macOS the command runs
        # under the same Seatbelt profile as the out-of-process child (no
        # outbound network, writes only inside the workspace, the $HOME read
        # jail). The profile is written to the session root — outside the
        # workspace, so a sandboxed command cannot loosen its own jail. Where
        # the platform has no OS sandbox, argv is None and the command runs as
        # before (`shell=True`), env scrubbing the only containment — a state
        # only reachable behind the explicit PYHARNESS_ALLOW_UNSANDBOXED opt-in,
        # since the session's kernel refuses to start on such a platform without
        # it (see remote/sandbox.py:check_unsandboxed_platform); a future Linux
        # path plugs into sandboxed_shell_argv.
        argv = sandboxed_shell_argv(cmd, self.ws.root, self.ws.dir)
        try:
            proc = subprocess.run(
                argv if argv is not None else cmd,
                shell=argv is None,
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
