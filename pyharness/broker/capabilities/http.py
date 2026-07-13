from __future__ import annotations

import base64
import re
from html.parser import HTMLParser
from importlib import import_module
from uuid import uuid4

from ...core.workspace import Workspace
from ...security.policy import ActionCategory
from ...security.sink import SecretSink
from ...security.vault import Vault
from ...util import MAX_OUTPUT, truncate

# Methods that mutate remote state. The default policy gates these behind human
# approval (see session.py / _is_mutating_http); reads (GET/HEAD/OPTIONS) stay
# free. Kept here so the capability and the policy predicate share one list.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Tags whose text is markup noise, not content, and block-level tags that mark a
# line boundary — used by the HTML->text reducer below.
_HTML_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})
_HTML_BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "header", "footer", "nav", "main", "aside",
     "li", "ul", "ol", "table", "tr", "br", "hr", "h1", "h2", "h3", "h4", "h5",
     "h6", "blockquote", "pre", "figure", "figcaption"}
)


class _TextExtractor(HTMLParser):
    """Collect visible text from an HTML document, dropping script/style/head
    noise and inserting newlines at block boundaries so structure survives."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _HTML_BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _HTML_SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _HTML_BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        # A newline inside text content is just whitespace in HTML, so collapse
        # every run to a single space here; the only real line breaks are the
        # "\n" sentinels emitted at block boundaries above.
        if self._skip_depth == 0:
            self._chunks.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    """Reduce an HTML document to readable text: drop script/style/head noise,
    keep block boundaries as newlines, and collapse whitespace. Stdlib only — a
    lightweight stand-in for a full readability pass, enough to keep an agent
    (and its context window) from drowning in markup. Falls back to the raw input
    if parsing fails."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not fail the fetch
        return html
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", parser.text())  # trim spaces around breaks
    text = re.sub(r"\n{3,}", "\n\n", text)  # squeeze blank-line runs to one
    return text.strip()


def _apply_secret_auth(headers: dict, params: dict, *, secret: str, style: str, name, user) -> None:
    """Attach a resolved secret to the outgoing request the way `auth_style`
    selects. Mirrors the four styles `web_fetch` has always supported; the secret
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
    `session_id=None` for a one-shot throwaway client, which is all `web_fetch`
    now needs.

    Secrets follow the same use-but-don't-view rule as `web_fetch`: the agent
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
        """Describe a gated request for the approver: method + target url, plus the
        body field *names* (never values — those can be workspace data). A DELETE
        is classed IRREVERSIBLE; other mutating methods are OUTWARD."""
        method = str(kwargs.get("method") or (args[1] if len(args) >= 2 else "")).upper()
        url = kwargs.get("url") or (args[2] if len(args) >= 3 else "")
        body = kwargs.get("json") if kwargs.get("json") is not None else kwargs.get("data")
        summary = f"{method} {url}"
        if isinstance(body, dict) and body:
            summary += f" (body: {', '.join(map(str, body))})"
        category = ActionCategory.IRREVERSIBLE if method == "DELETE" else ActionCategory.OUTWARD
        return category, summary

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
    ) -> dict:
        """Perform one request on `session_id` (or a throwaway client if None).

        `auth` names a vault secret injected per `auth_style`
        (bearer/header/query/basic); `secret_fields` maps a body field name to a
        secret name and injects into the `json`/`data` body. `files` is a list of
        `[field, workspace_path]`; each file is read parent-side (path confined to
        the workspace) so agent code never handles the bytes. `extract_text`
        reduces an HTML response to readable text before truncation (no-op for
        non-HTML responses)."""
        headers = dict(headers or {})
        params = dict(params or {})
        sink = SecretSink(self.vault)

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
            )
        finally:
            if transient:
                client.close()

        elapsed = getattr(resp, "elapsed", None)
        text = resp.text
        # Reduce HTML to readable text before truncating, so the 10k cap keeps
        # article content rather than the megabytes of <style>/<script> that lead
        # a modern page. Extraction is opt-in (web_fetch sets it) and only fires
        # for HTML, so JSON/API and raw-file reads pass through verbatim.
        if extract_text and "html" in resp.headers.get("content-type", "").lower():
            text = html_to_text(text)
        # Mask any injected secret out of every string the agent reads back: a
        # query-string `auth` secret can survive into the final url, and a
        # `secret_fields` body value can be echoed in the response text/headers.
        return sink.redacted(
            {
                "status": resp.status_code,
                "url": str(resp.url),
                "headers": dict(resp.headers),
                "text": truncate(text),
                "truncated": len(text) > MAX_OUTPUT,
                "elapsed_ms": round(elapsed.total_seconds() * 1000) if elapsed else None,
            }
        )
