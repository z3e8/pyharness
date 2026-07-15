from __future__ import annotations

import base64
from importlib import import_module
from urllib.parse import urlsplit
from uuid import uuid4

from ...core.workspace import Workspace
from ...security.egress import check_url
from ...security.grants import GrantScope
from ...security.policy import ActionCategory
from ...security.sink import SecretSink
from ...security.vault import Vault
from .page import extract_content, html_to_text, parse_affordances
from .payload import deliver, is_textual

# Re-exported so `from ...http import html_to_text` (and existing callers/tests)
# keep working now that the HTML-perception layer lives in page.py.
__all__ = ["HttpSessionCapability", "MUTATING_METHODS", "html_to_text"]

# Methods that mutate remote state. The default policy gates these behind human
# approval (see session.py / _is_mutating_http); reads (GET/HEAD/OPTIONS) stay
# free. Kept here so the capability and the policy predicate share one list.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Mutating methods a scoped grant may cover. DELETE is excluded: it is classed
# IRREVERSIBLE (see preview()) and must re-prompt every time, so it never yields a
# grant scope.
_GRANTABLE_METHODS = MUTATING_METHODS - {"DELETE"}


def _apply_secret_auth(headers: dict, params: dict, *, secret: str, style: str, name, user) -> None:
    """Attach a resolved secret to the outgoing request the way `auth_style`
    selects. Mirrors the four styles `web.fetch` has always supported; the secret
    is already cleartext here (resolved parent-side), never seen by agent code."""
    if style == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif style == "header":
        if not name:
            raise ValueError("auth_style='header' requires auth_name")
        headers[name] = secret
    elif style == "query":
        if not name:
            raise ValueError("auth_style='query' requires auth_name")
        params[name] = secret
    elif style == "basic":
        token = base64.b64encode(f"{user or ''}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    else:
        raise ValueError(f"unknown auth_style {style!r}")


class HttpSessionCapability:
    """Stateful HTTP the agent opens once and reuses across cells.

    The live `httpx.Client` (cookie jar, connection pool) lives here in the
    parent, keyed by a session-id string; the agent only ever holds the id.
    child->parent calls are JSON, so a live client could not cross the wire even
    if we wanted it to — the id is the handle. `request` also accepts
    `session_id=None` for a one-shot throwaway client, which is all `web.fetch`
    now needs.

    Secrets follow the same use-but-don't-view rule as `web.fetch`: the agent
    passes a secret *name*, this capability resolves the cleartext parent-side and
    injects it at the point of use (header / query / basic / body field). The
    audit log records the name (via `summarize_args`), never the value.

    Every call returns a structured result (status / url / headers / text /
    elapsed_ms), not a bare string, so the agent can check its own work — assert
    on status, or re-request to confirm an effect landed."""

    name = "http"

    def __init__(self, workspace: Workspace, vault: Vault | None = None):
        self.ws = workspace
        self.vault = vault
        self._clients: dict[str, object] = {}

    def exports(self) -> dict:
        return {
            "open_session": self.open_session,
            "request": self.request,
            "close_session": self.close_session,
        }

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """Describe a gated request for the approver: method + target url, any vault
        secret being attached (name and style — never the value), and the body field
        *names* (never values — those can be workspace data). Naming the credential
        lets the human catch a token being sent to the wrong host. A DELETE is
        classed IRREVERSIBLE; other mutating methods are OUTWARD."""
        method = str(kwargs.get("method") or (args[1] if len(args) >= 2 else "")).upper()
        url = kwargs.get("url") or (args[2] if len(args) >= 3 else "")
        body = kwargs.get("json") if kwargs.get("json") is not None else kwargs.get("data")
        summary = f"{method} {url}"
        creds: list[str] = []
        if kwargs.get("auth"):
            creds.append(f"auth={kwargs['auth']} via {kwargs.get('auth_style', 'bearer')}")
        if kwargs.get("secret_fields"):
            creds.append("secret fields: " + ", ".join(map(str, kwargs["secret_fields"])))
        if creds:
            summary += " [" + "; ".join(creds) + "]"
        if isinstance(body, dict) and body:
            summary += f" (body: {', '.join(map(str, body))})"
        category = ActionCategory.IRREVERSIBLE if method == "DELETE" else ActionCategory.OUTWARD
        return category, summary

    def scope(self, op: str, args: tuple, kwargs: dict) -> GrantScope | None:
        """The grant key for a gated request: action-class "http" plus the target
        host, parsed from the same method/url args `preview()` uses. Grantable per
        host for mutating (non-DELETE) methods and for any secret-carrying request
        (so authenticated reads to a vetted host don't re-prompt). DELETE
        (IRREVERSIBLE) and any url without a parseable host yield None (not
        grantable → always prompts)."""
        if op != "request":
            return None
        method = str(kwargs.get("method") or (args[1] if len(args) >= 2 else "")).upper()
        carries_secret = bool(kwargs.get("auth") or kwargs.get("secret_fields"))
        if method == "DELETE" or (method not in _GRANTABLE_METHODS and not carries_secret):
            return None
        url = kwargs.get("url") or (args[2] if len(args) >= 3 else "")
        host = urlsplit(url).hostname if url else None
        return GrantScope("http", host.lower()) if host else None

    def open_session(self) -> str:
        """Open a persistent session and return its id. Reuse the id across cells
        and calls; cookies set by the server persist on it until `close_session`."""
        httpx = import_module("httpx")
        session_id = uuid4().hex
        self._clients[session_id] = httpx.Client(follow_redirects=True, timeout=30)
        return session_id

    def close_session(self, session_id: str) -> str:
        client = self._clients.pop(session_id, None)
        if client is not None:
            client.close()
        return f"closed http session {session_id}"

    def close_all(self) -> None:
        """Tear down every open client; called from Session.close()."""
        for client in self._clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._clients.clear()

    def request(
        self,
        session_id: str | None,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        json: object | None = None,
        data: dict | None = None,
        files: list | None = None,
        auth: str | None = None,
        auth_style: str = "bearer",
        auth_name: str | None = None,
        auth_user: str | None = None,
        secret_fields: dict | None = None,
        extract_text: bool = False,
        save: str | None = None,
    ) -> dict:
        """Perform one request on `session_id` (or a throwaway client if None).

        `auth` names a vault secret injected per `auth_style`
        (bearer/header/query/basic); `secret_fields` maps a body field name to a
        secret name and injects into the `json`/`data` body. `files` is a list of
        `[field, workspace_path]`; each file is read parent-side (path confined to
        the workspace) so agent code never handles the bytes. `extract_text`
        reduces an HTML response to readable text (no-op for non-HTML responses).

        The body comes back whole, not capped. A textual response rides back
        inline as `text` (parse it in the kernel); a binary one — or an explicit
        `save="path"`, or text past the inline ceiling — is written to the
        workspace and returned as `path`/`bytes`/`preview` with `text=None`. Either
        way the full data is available: inline as a variable, or on disk for the
        agent's own Python to read. See `payload.deliver`."""
        check_url(url)  # SSRF guard: no link-local/metadata (or private, if strict) targets
        headers = dict(headers or {})
        params = dict(params or {})
        sink = SecretSink(self.vault)
        # A request that carries a credential must not follow redirects: httpx
        # strips only `Authorization` across origins, so a custom-header (`header`
        # style) or body-field (`secret_fields`) secret would be resent verbatim to
        # an attacker-controlled `Location` — an exfil path the approval prompt (it
        # shows only the initial url) never sees. Fail safe by returning the 3xx;
        # the agent can re-issue against the resolved url, re-deciding auth.
        carries_secret = bool(auth or secret_fields)

        if auth:
            _apply_secret_auth(
                headers,
                params,
                secret=sink.resolve(auth),
                style=auth_style,
                name=auth_name,
                user=auth_user,
            )

        if secret_fields:
            body = json if json is not None else data
            if not isinstance(body, dict):
                raise ValueError("secret_fields requires a dict `json` or `data` body")
            for field, secret_name in secret_fields.items():
                body[field] = sink.resolve(secret_name)

        built_files = None
        if files:
            built_files = [
                (field, (self.ws.path(path).name, self.ws.path(path).read_bytes()))
                for field, path in files
            ]

        transient = session_id is None
        if transient:
            httpx = import_module("httpx")
            client = httpx.Client(follow_redirects=True, timeout=30)
        else:
            client = self._clients.get(session_id)
            if client is None:
                raise KeyError(f"no open http session {session_id!r}")

        try:
            resp = client.request(
                method,
                url,
                params=params or None,
                headers=headers or None,
                json=json,
                data=data,
                files=built_files,
                follow_redirects=not carries_secret,
            )
        finally:
            if transient:
                client.close()

        elapsed = getattr(resp, "elapsed", None)
        content_type = resp.headers.get("content-type", "")
        text = resp.text
        title: str | None = None
        links: list[dict] = []
        forms: list[dict] = []
        # Reduce HTML to readable content and pull the links/forms the agent needs
        # to navigate and fill (parse_affordances runs on the raw markup, before
        # `text` is reassigned to the reduced content). Extraction is opt-in
        # (web.fetch sets it) and only fires for HTML, so JSON/API and raw-file
        # reads pass through verbatim. trafilatura gives clean markdown; the stdlib
        # reducer is the fallback when it declines (returns None) on thin markup.
        if extract_text and "html" in content_type.lower():
            title, links, forms = parse_affordances(text, str(resp.url))
            reduced = extract_content(text)
            text = reduced if reduced is not None else html_to_text(text)
        # Mask any injected secret before it can reach agent code *or* the disk: a
        # query-string `auth` secret can survive into the final url, and a
        # `secret_fields` body value can be echoed in the response text/headers.
        text = sink.redact(text)
        content = None if is_textual(content_type) else sink.redact_bytes(resp.content)
        body = deliver(
            ws=self.ws,
            content_type=content_type,
            text=text,
            content=content,
            save=save,
            default_name=url.rstrip("/").rsplit("/", 1)[-1] or "body",
        )
        # links/forms/title are small and ride inline on the result dict even when
        # the body itself spilled to disk — the affordances are exactly what the
        # agent needs to orient on a page too big to inline. sink.redacted recurses
        # into these lists so a secret echoed into an href or field never survives.
        return sink.redacted(
            {
                "status": resp.status_code,
                "url": str(resp.url),
                "headers": dict(resp.headers),
                "content_type": content_type,
                "elapsed_ms": round(elapsed.total_seconds() * 1000) if elapsed else None,
                "title": title,
                "links": links,
                "forms": forms,
                **body,
            }
        )
