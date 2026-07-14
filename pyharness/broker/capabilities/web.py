from __future__ import annotations

from .http import HttpSessionCapability
from .page import render_page_map


class WebCapability:
    name = "web"

    def __init__(self, llm, http: HttpSessionCapability, tier: str = "cheap"):
        self.llm = llm
        self.http = http
        self.tier = tier

    def exports(self) -> dict:
        return {"search": self.search, "fetch": self.fetch}

    def search(self, query: str, tier: str | None = None) -> str:
        """Search the web and return a text answer with sources, via Anthropic's
        server-side search tool (no separate search API key). `tier` selects the
        model that runs the search; defaults to the capability's tier."""
        return self.llm.web_search(query, tier=tier or self.tier)

    def fetch(
        self,
        url: str,
        auth: str | None = None,
        auth_style: str = "bearer",
        auth_name: str | None = None,
        user: str | None = None,
        save: str | None = None,
    ) -> str:
        """Fetch a URL (stateless GET) and return it as a readable page map: an
        HTML page is reduced to clean markdown content, followed by a `## FORMS`
        section (each form's action/method and every field) and a `## LINKS`
        section (the links to navigate) — so the agent can see what to click and
        fill, not just prose. JSON and other non-HTML responses pass through
        verbatim. The whole body is returned, not a capped head. A thin wrapper
        over the HTTP session capability's one-shot `request`; `auth` names a vault
        secret injected parent-side and never returned to the caller. See `request`
        for the `auth_style` options, and for the structured `links`/`forms` lists
        if you want to drive HTTP directly instead of reading the markdown.

        Only static HTML is parsed — links and forms rendered by JavaScript need
        the `browser` capability.

        Pass `save="path"` (or fetch a binary body) and the full content lands in
        the workspace instead; the return is then a note pointing at the file plus
        the same FORMS/LINKS map, so even a page too big to inline stays
        navigable."""
        result = self.http.request(
            None,
            "GET",
            url,
            auth=auth,
            auth_style=auth_style,
            auth_name=auth_name,
            auth_user=user,
            extract_text=True,
            save=save,
        )
        if result["text"] is not None:
            return render_page_map(
                result["text"], result["links"], result["forms"], result["title"]
            )
        note = (
            f"[saved {result['bytes']} bytes to {result['path']} "
            f"({result['content_type']}) — read/parse it from the workspace]"
        )
        affordances = render_page_map("", result["links"], result["forms"])
        return f"{note}\n\n{affordances}" if affordances else note
