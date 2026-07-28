from __future__ import annotations

import re
import subprocess

from ...core.session_venv import SessionVenv
from ...security.env import minimal_environ
from ...security.policy import ActionCategory
from ..remote.sandbox import sandboxed_install_argv


class PackagesCapability:
    name = "packages"

    def __init__(self, venv: SessionVenv) -> None:
        self._venv = venv

    def exports(self) -> dict:
        return {"install": self.install, "list_installed": self.list_installed}

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """Installing fetches from PyPI and adds executable code to the session —
        OUTWARD (a remote fetch under a supply-chain sign-off)."""
        package = kwargs.get("package") or (args[0] if args else "?")
        return ActionCategory.OUTWARD, f"pip install {package}"

    def install(self, package: str) -> str:
        # Refuse to (re)install pyharness itself. A capability's own code (the
        # browser lane, say) runs HOST-side, not in this session venv, so a
        # self-install here can never enable it — it only pulls a second copy of
        # the harness from PyPI onto the child's path where nothing imports it.
        # A missing optional dependency is a host setup step; point there instead
        # of letting the agent loop on an install that structurally cannot work.
        if (
            re.split(r"[\[=<>~!;\s]", package.strip(), maxsplit=1)[0].lower()
            == "pyharness"
        ):
            return (
                "refusing to install pyharness into the session: capability code "
                "(e.g. the browser lane) runs in the host process, not this "
                "session venv, so an in-session install cannot enable it. A "
                "missing optional dependency is a host setup step — ask the "
                "operator to run `make browser`, not you."
            )
        site = self._venv.site_packages()
        if site is None:
            return "package installation requires out-of-process mode"
        venv_dir = self._venv.dir
        pip = venv_dir / "bin" / "pip"
        # A package's `setup.py`/build hook runs arbitrary code at install time —
        # so pip must not inherit the parent environment, or a malicious package
        # could read ANTHROPIC_API_KEY, the vault passphrase, or any
        # PYHARNESS_SECRET_*. It gets the same minimal allowlist env the child
        # kernel and shell.bash use (PATH/HOME/TMPDIR is all pip legitimately
        # needs; see security/env.py).
        env = minimal_environ()
        # ...and it runs under the OS sandbox as well, for the same reason
        # `shell.bash` does: this executes in the privileged parent, so without a
        # wrapper a build hook would have the operator's full OS reach. The
        # install profile allows the network (pip must reach the index) but keeps
        # the $HOME read jail and confines writes to the venv plus a scratch dir.
        scratch = venv_dir.parent / "install-tmp"
        scratch.mkdir(exist_ok=True)
        env["TMPDIR"] = str(scratch)
        # Without this pip would try to cache under $HOME, which the read jail
        # hides and the write jail denies.
        env["PIP_NO_CACHE_DIR"] = "1"
        argv = [str(pip), "install", package]
        wrapped = sandboxed_install_argv(argv, venv_dir.parent, [venv_dir, scratch])
        result = subprocess.run(
            wrapped or argv,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        return (result.stdout + result.stderr).strip() or f"installed {package}"

    def list_installed(self) -> str:
        if self._venv.dir is None:
            return "no per-session venv active"
        pip = self._venv.dir / "bin" / "pip"
        result = subprocess.run(
            [str(pip), "list"], capture_output=True, text=True, env=minimal_environ()
        )
        return result.stdout.strip()
