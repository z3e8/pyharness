from __future__ import annotations

import os

from . import exa
from .http import HttpSessionCapability
from .page import render_page_map


class WebCapability:
    name = "web"

    def __init__(self, llm, http: HttpSessionCapability, tier: str = "cheap"):
        self.llm = llm
        self.http = http
        self.tier = tier

    def exports(self) -> dict:
        return {
            "search_results": self.search_results,
            "fetch": self.fetch,
        }

    def search_results(self, query: str, num_results: int = 10) -> list[dict]:
        """Search the web and return a *raw ranked list* to fan out over — each
        item a dict `{title, url, snippet, published_date, author, score}`. Use
        this for research: fetch the URLs with `web.fetch`/`http.request` and rank
        by `score`.

        `snippet` is a short relevant excerpt, not the page body — fetch the url
        for the full content. `num_results` is clamped to 1–100 (default 10).

        Backed by Exa; needs `EXA_API_KEY` in the environment. The key is resolved
        parent-side and never reaches agent code. Note: each search carries a small
        per-query dollar cost at Exa that is not recorded to the budget."""
        api_key = os.environ.get("EXA_API_KEY")
        if not api_key:
            raise RuntimeError(
                "EXA_API_KEY not set — add it to .env to use web.search_results"
            )
        return exa.search(query, api_key=api_key, num_results=num_results)

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
