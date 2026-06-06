from __future__ import annotations

import os


class Vault:
    """Secret store with one hard rule: no capability exposed to agent code ever
    returns a secret's cleartext.

    The agent references a secret by name; the broker/tools resolve it here, in
    the parent, and inject it at the point of use (see web.web_fetch). `get` is
    deliberately NOT placed in the agent's kernel namespace.

    V1 backend: an in-memory dict plus environment variables
    (PYHARNESS_SECRET_<NAME>). Swap for 1Password / Bitwarden / Hashicorp later
    behind this same interface.
    """

    def __init__(self, secrets: dict[str, str] | None = None, env_prefix: str = "PYHARNESS_SECRET_"):
        self._secrets = dict(secrets or {})
        self._env_prefix = env_prefix

    def get(self, name: str) -> str:
        if name in self._secrets:
            return self._secrets[name]
        env = os.environ.get(self._env_prefix + name.upper())
        if env is not None:
            return env
        raise KeyError(f"secret {name!r} not found")
