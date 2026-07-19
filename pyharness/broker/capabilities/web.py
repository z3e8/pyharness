from __future__ import annotations

import os
from urllib.parse import urlsplit

from ...security.egress import EgressBlocked
from ...security.grants import GrantScope
from ...security.policy import ActionCategory
from . import exa
from .http import HttpSessionCapability
from .page import render_page_map, thin_extraction_warning


class WebCapability:
    name = "web"

    def __init__(self, http: HttpSessionCapability):
        self.http = http

    def exports(self) -> dict:
        return {
            "search_results": self.search_results,
            "fetch": self.fetch,
        }

    @staticmethod
    def _fetch_auth(args: tuple, kwargs: dict):
        """(url, auth-name-or-None, auth-style) from a `fetch` call in either
        convention. `fetch(url, auth=..., auth_style=...)` — auth is the 2nd
        positional, auth_style the 3rd."""
        url = kwargs.get("url") or (args[0] if args else "")
        auth = kwargs.get("auth") or (args[1] if len(args) >= 2 else None)
        style = kwargs.get("auth_style") or (args[2] if len(args) >= 3 else "bearer")
        return url, auth, style

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """Only `fetch` is gated, and only when it attaches a vault secret (see
        session._request_carries_secret). Name the credential and the target so the
        human can catch a token headed for the wrong host — the value is never
        shown (auth is a secret *name*)."""
        url, auth, style = self._fetch_auth(args, kwargs)
        summary = f"GET {url}"
        if auth:
            summary += f" [auth={auth} via {style}]"
        return ActionCategory.OUTWARD, summary

    def scope(self, op: str, args: tuple, kwargs: dict) -> GrantScope | None:
        """Grant key for a secret-carrying fetch: action-class "http" (shared with
        the HTTP capability, so one host grant covers both) plus the target host.
        A fetch with no secret is not gated, so it needs no scope."""
        if op != "fetch":
            return None
        url, auth, _ = self._fetch_auth(args, kwargs)
        if not auth:
            return None
        host = urlsplit(url).hostname if url else None
        return GrantScope("http", host.lower()) if host else None

    def search_results(self, query: str, num_results: int = 10) -> list[dict]:
        """Search the web and return a *raw ranked list* to fan out over — each
        item a dict `{title, url, snippet, published_date, author, score}`. Use
        this for research: fetch the URLs with `web.fetch`/`http.request` and rank
        by `score`.

        `snippet` is a short relevant excerpt, not the page body — fetch the url
        for the full content. `num_results` is clamped to 1–100 (default 10).

        Backed by Exa; needs `EXA_API_KEY` in the environment. The key is resolved
        parent-side and never reaches agent code. Note: each search carries a small
        per-query dollar cost at Exa that is not recorded to the budget.

        Unavailable in a host-scoped session (e.g. a child spawned with
        `allowed_hosts=...`): the query itself travels to the search provider,
        which is outside the scope — and a free-text query is a classic
        exfiltration channel. Fetch the allowed hosts directly instead."""
        # Scope lives on the shared HTTP substrate — one source of truth for
        # the session's whole web/http reach. (http is None only in tests that
        # exercise search alone — an unscoped configuration.)
        scope = self.http.allowed_hosts if self.http is not None else None
        if scope is not None:
            raise EgressBlocked(
                "web search is unavailable under a host scope: the query would "
                "go to the search provider, outside the allowed hosts "
                f"({', '.join(sorted(scope))})"
            )
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
        the `browser` capability. When extraction looks thin (almost no body
        text but plenty of links — the shape of a JS-rendered shell), the page
        map starts with a single `[warning: extraction looks thin …]` line;
        treat the content as suspect and reach for the browser instead.

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
            page = render_page_map(
                result["text"], result["links"], result["forms"], result["title"]
            )
            # Flag likely JS-rendered shells inline (non-HTML bodies carry no
            # links, so they can never trigger). The saved-to-disk path below is
            # exempt: its inline body is empty by design, not thin extraction.
            warning = thin_extraction_warning(result["text"], result["links"])
            return f"{warning}\n\n{page}" if warning else page
        note = (
            f"[saved {result['bytes']} bytes to {result['path']} "
            f"({result['content_type']}) — read/parse it from the workspace]"
        )
        affordances = render_page_map("", result["links"], result["forms"])
        return f"{note}\n\n{affordances}" if affordances else note
