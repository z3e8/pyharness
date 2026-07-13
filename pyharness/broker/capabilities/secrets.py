from __future__ import annotations

from ...security.vault import Vault


class SecretsCapability:
    """Lets the agent discover which credentials it can reference — names only.

    The values never leave the parent: this exposes `secrets()` returning the
    list of available secret names. The agent passes one of these names to a
    capability's auth argument (e.g. web.fetch(url, auth="github")); the broker
    injects the cleartext parent-side at the point of use."""

    name = "vault"

    def __init__(self, vault: Vault):
        self.vault = vault

    def exports(self) -> dict:
        return {"secrets": self.list_names}

    def list_names(self) -> list[str]:
        """Return the NAMES of secrets available to inject (never the values).
        Reference one by name in a capability's `auth` argument."""
        return self.vault.names()
