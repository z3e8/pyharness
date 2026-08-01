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
        """OUTWARD because `bash` runs **parent-side**, not because running a
        program is inherently worse than running a cell.

        Worth being precise, since the obvious rationale ("an arbitrary program
        is an arbitrary program") does not survive contact: a cell's own
        `subprocess.run` is an arbitrary program too, and it is *equally*
        contained — the child is already jailed and the OS sandbox is inherited
        across `exec` (macOS Seatbelt, Linux Landlock+seccomp), on top of the
        reduced environment `reduce_environ()` installs. So the gate is not
        buying containment over a cell.

        What it buys is that this is the one exec path whose jail is applied
        **by construction** rather than inherited: `bash` executes in the
        unsandboxed parent, so `sandboxed_shell_argv` has to wrap it correctly
        per platform, and returns None — no jail at all, env scrubbing only — on
        a platform with no OS sandbox. A wrap that must be right is worth a human
        looking at the command line; an inherited jail is not. See the residual
        risk in docs/explanation/threat-model.md: local execution inside the box
        is contained but deliberately unaudited."""
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
