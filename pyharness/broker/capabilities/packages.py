from __future__ import annotations

import subprocess

from ...core.session_venv import SessionVenv


class PackagesCapability:
    name = "packages"

    def __init__(self, venv: SessionVenv) -> None:
        self._venv = venv

    def exports(self) -> dict:
        return {"install": self.install, "list_installed": self.list_installed}

    def install(self, package: str) -> str:
        site = self._venv.site_packages()
        if site is None:
            return "package installation requires out-of-process mode"
        pip = self._venv.dir / "bin" / "pip"
        result = subprocess.run(
            [str(pip), "install", package],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return (result.stdout + result.stderr).strip() or f"installed {package}"

    def list_installed(self) -> str:
        if self._venv.dir is None:
            return "no per-session venv active"
        pip = self._venv.dir / "bin" / "pip"
        result = subprocess.run([str(pip), "list"], capture_output=True, text=True)
        return result.stdout.strip()
