"""Default-deny subprocess environment (security/env.py). Subprocesses reachable
by agent code — child kernel, shell.bash, local MCP servers — start from a
minimal allowlist, so a secret only has to be *absent from the allowlist* to be
protected, not *present on a denylist*."""
from __future__ import annotations

from pyharness import Workspace
from pyharness.broker.capabilities.shell import ShellCapability
from pyharness.security.env import PASSTHROUGH_ENV, minimal_environ, reduce_environ


def test_minimal_environ_is_allowlist(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws")  # never on any denylist
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.delenv(PASSTHROUGH_ENV, raising=False)
    env = minimal_environ()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "DATABASE_URL" not in env
    assert env["LC_ALL"] == "en_US.UTF-8"  # LC_* prefix is allowlisted
    assert "PATH" in env and "HOME" in env


def test_passthrough_admits_named_vars(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setenv("PROJ_CONFIG", "y")
    monkeypatch.setenv(PASSTHROUGH_ENV, "DATABASE_URL, PROJ_CONFIG")
    env = minimal_environ()
    assert env["DATABASE_URL"] == "postgres://x"
    assert env["PROJ_CONFIG"] == "y"


def test_passthrough_cannot_resurrect_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setenv("PYHARNESS_SECRET_GITHUB", "gh")
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "pw")
    monkeypatch.setenv(
        PASSTHROUGH_ENV, "ANTHROPIC_API_KEY,PYHARNESS_SECRET_GITHUB,PYHARNESS_VAULT_PASSPHRASE"
    )
    env = minimal_environ()
    assert "ANTHROPIC_API_KEY" not in env
    assert "PYHARNESS_SECRET_GITHUB" not in env
    assert "PYHARNESS_VAULT_PASSPHRASE" not in env


def test_reduce_environ_shrinks_own_process(monkeypatch):
    import os

    fake = {"PATH": "/bin", "HOME": "/h", "SECRET_THING": "x"}
    monkeypatch.setattr(os, "environ", fake)
    reduce_environ()
    assert os.environ == {"PATH": "/bin", "HOME": "/h"}


def test_bash_runs_on_minimal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CUSTOM_TOKEN", "leakme")
    monkeypatch.delenv(PASSTHROUGH_ENV, raising=False)
    bash = ShellCapability(Workspace(tmp_path)).bash
    out = bash("printenv CUSTOM_TOKEN || echo MISSING")
    assert "leakme" not in out and "MISSING" in out
    monkeypatch.setenv(PASSTHROUGH_ENV, "CUSTOM_TOKEN")
    assert "leakme" in bash("printenv CUSTOM_TOKEN")
