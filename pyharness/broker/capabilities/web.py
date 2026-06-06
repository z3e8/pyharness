from __future__ import annotations

from importlib import import_module

from ...security.vault import Vault
from ...util import truncate


class WebCapability:
    name = "web"

    def __init__(self, llm, vault: Vault | None = None, tier: str = "mid"):
        self.llm = llm
        self.vault = vault
        self.tier = tier

    def exports(self) -> dict:
        return {"web_search": self.web_search, "web_fetch": self.web_fetch}

    def web_search(self, query: str, tier: str | None = None) -> str:
        return self.llm.web_search(query, tier=tier or self.tier)

    def web_fetch(self, url: str, auth: str | None = None) -> str:
        """Fetch a URL. `auth` names a vault secret to inject as a bearer token;
        the secret's cleartext is never returned to the caller."""
        httpx = import_module("httpx")
        headers = {"User-Agent": "pyharness/0.1"}
        if auth:
            if not self.vault:
                raise RuntimeError("no vault configured for auth injection")
            headers["Authorization"] = f"Bearer {self.vault.get(auth)}"
        resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
        return truncate(resp.text)
