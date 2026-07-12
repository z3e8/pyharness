from __future__ import annotations

from .http import HttpSessionCapability


class WebCapability:
    name = "web"

    def __init__(self, llm, http: HttpSessionCapability, tier: str = "cheap"):
        self.llm = llm
        self.http = http
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
        """Fetch a URL (stateless GET). A thin wrapper over the HTTP session
        capability's one-shot `request`; `auth` names a vault secret injected
        parent-side and never returned to the caller. See `request` for the
        `auth_style` options."""
        result = self.http.request(
            None,
            "GET",
            url,
            auth=auth,
            auth_style=auth_style,
            auth_name=auth_name,
            auth_user=user,
        )
        return result["text"]
