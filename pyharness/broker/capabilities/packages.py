from __future__ import annotations

import subprocess

from ...core.session_venv import SessionVenv
from ...security.env import minimal_environ
from ...security.policy import ActionCategory


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
        site = self._venv.site_packages()
        if site is None:
            return "package installation requires out-of-process mode"
        pip = self._venv.dir / "bin" / "pip"
        # A package's `setup.py`/build hook runs arbitrary code at install time —
        # so pip must not inherit the parent environment, or a malicious package
        # could read ANTHROPIC_API_KEY, the vault passphrase, or any
        # PYHARNESS_SECRET_*. It gets the same minimal allowlist env the child
        # kernel and shell.bash use (PATH/HOME/TMPDIR is all pip legitimately
        # needs; see security/env.py).
        result = subprocess.run(
            [str(pip), "install", package],
            capture_output=True,
            text=True,
            timeout=120,
            env=minimal_environ(),
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
