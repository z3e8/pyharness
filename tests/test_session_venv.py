"""SessionVenv: the per-session venv site-packages probe."""

from __future__ import annotations

import subprocess

from pyharness.core.session_venv import SessionVenv


def test_site_packages_times_out_instead_of_hanging(tmp_path, monkeypatch):
    venv = SessionVenv()
    venv.dir = tmp_path / "venv"  # pretend it was created

    def _hang(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="python", timeout=30)

    monkeypatch.setattr(subprocess, "run", _hang)
    # A wedged venv python must degrade to "no path", never block kernel start.
    assert venv.site_packages() is None
