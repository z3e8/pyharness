from __future__ import annotations

import subprocess
import venv
from pathlib import Path


class SessionVenv:
    """A per-session virtual environment for package installation.

    Shared between PackagesCapability (installs into it) and RemoteKernel
    (passes its site-packages path to the child at startup). Created lazily
    when ensure_created() is first called; deleted by the caller (RemoteKernel
    removes the parent directory on close)."""

    def __init__(self) -> None:
        self.dir: Path | None = None

    def ensure_created(self, parent: Path) -> Path:
        if self.dir is None:
            self.dir = parent / "venv"
            venv.create(str(self.dir), with_pip=True, system_site_packages=True)
        return self.dir

    def site_packages(self) -> Path | None:
        if self.dir is None:
            return None
        result = subprocess.run(
            [str(self.dir / "bin" / "python"), "-c",
             "import sysconfig; print(sysconfig.get_path('purelib'))"],
            capture_output=True,
            text=True,
        )
        return Path(result.stdout.strip()) if result.returncode == 0 else None
