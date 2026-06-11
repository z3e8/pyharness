from __future__ import annotations

import base64
from importlib import import_module

from ...security.vault import Vault
from ...util import truncate


class WebCapability:
    name = "web"

    def __init__(self, llm, vault: Vault | None = None, tier: str = "cheap"):
        self.llm = llm
        self.vault = vault
        self.tier = tier

    def exports(self) -> dict:
        return {"web_search": self.web_search, "web_fetch": self.web_fetch}

    def web_search(self, query: str, tier: str | None = None) -> str:
        return self.llm.web_search(query, tier=tier or self.tier)

    def web_fetch(
        self,
        url: str,
        auth: str | None = None,
        auth_style: str = "bearer",
        auth_name: str | None = None,
        user: str | None = None,
    ) -> str:
        """Fetch a URL. `auth` names a vault secret, resolved and injected
        parent-side; its cleartext is never returned to the caller. `auth_style`
        selects how the secret is attached:

          - "bearer" (default): header `Authorization: Bearer <secret>`
          - "header": custom header named `auth_name`, value = `<secret>`
            (e.g. auth_name="X-API-Key")
          - "query":  query parameter named `auth_name`, value = `<secret>`
          - "basic":  header `Authorization: Basic base64(user:<secret>)`
        """
        httpx = import_module("httpx")
        headers = {"User-Agent": "pyharness/0.1"}
        params: dict[str, str] = {}
        if auth:
            if not self.vault:
                raise RuntimeError("no vault configured for auth injection")
            secret = self.vault.get(auth)
            if auth_style == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            elif auth_style == "header":
                if not auth_name:
                    raise ValueError("auth_style='header' requires auth_name")
                headers[auth_name] = secret
            elif auth_style == "query":
                if not auth_name:
                    raise ValueError("auth_style='query' requires auth_name")
                params[auth_name] = secret
            elif auth_style == "basic":
                token = base64.b64encode(f"{user or ''}:{secret}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
            else:
                raise ValueError(f"unknown auth_style {auth_style!r}")
        resp = httpx.get(url, headers=headers, params=params, timeout=30, follow_redirects=True)
        return truncate(resp.text)
