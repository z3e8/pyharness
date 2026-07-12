from __future__ import annotations

from importlib import import_module
from uuid import uuid4

from ...core.workspace import Workspace
from ...security.vault import Vault
from ...util import MAX_OUTPUT, truncate

# Page actions that change page/remote state. The default policy gates these
# behind human approval (see session.py); navigation and reads stay free. Kept
# here so the capability and the policy share one list.
MUTATING_ACTIONS = frozenset({"browser.click", "browser.fill", "browser.fill_secret"})


def _redact(text: str, secrets: set[str]) -> str:
    """Mask any secret *this session* injected out of text the agent reads back,
    so a credential typed into a page can never round-trip through agent-visible
    output. Only values injected here are masked — no need to enumerate the whole
    vault to scan for arbitrary secrets."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


class _BrowserSession:
    """One live page plus the set of secret cleartexts injected into it. The
    injected set drives read-back redaction; it never leaves the parent."""

    def __init__(self, browser, context, page):
        self.browser = browser
        self.context = context
        self.page = page
        self.injected: set[str] = set()

    def close(self) -> None:
        for obj in (self.context, self.browser):
            try:
                obj.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass


class BrowserCapability:
    """A scriptable browser the agent drives parent-side, one page per session id.

    Playwright (chromium) launches here in the parent — the unsandboxed side that
    already has network — exactly like `HttpSessionCapability`'s httpx client. The
    child only ever holds the session-id string; child->parent calls are JSON, so
    a live page could not cross the wire even if we wanted it to. The one
    persistent Playwright driver and the per-session pages live for the whole
    `Session`, so state persists across cells for free.

    Secrets follow the same use-but-don't-view rule as `web_fetch`: `fill_secret`
    takes a secret *name*, resolves the cleartext parent-side, types it into the
    field, and records it on the session so every subsequent read masks it. The
    agent never sees the value, and the audit log records the name (via
    `summarize_args`), never the value.

    Every method returns a structured result (url / title / status), not a bare
    string, so the agent can check its own work — assert on status, re-read to
    confirm an effect landed."""

    name = "browser"

    def __init__(self, workspace: Workspace, vault: Vault | None = None):
        self.ws = workspace
        self.vault = vault
        self._pw = None  # the persistent Playwright driver, started on first use
        self._sessions: dict[str, _BrowserSession] = {}

    def exports(self) -> dict:
        return {
            "open_browser": self.open_browser,
            "goto": self.goto,
            "click": self.click,
            "fill": self.fill,
            "fill_secret": self.fill_secret,
            "read_text": self.read_text,
            "screenshot": self.screenshot,
            "close_browser": self.close_browser,
        }

    def _driver(self):
        """Start (once) and return the Playwright driver. Playwright is an
        optional extra, imported lazily so the core install and the test suite
        never need it or a chromium binary."""
        if self._pw is None:
            try:
                sync_api = import_module("playwright.sync_api")
            except ImportError as exc:
                raise RuntimeError(
                    "browser support needs the optional dependency: install "
                    "'pyharness[browser]' and run 'playwright install chromium'"
                ) from exc
            self._pw = sync_api.sync_playwright().start()
        return self._pw

    def _session(self, session_id: str) -> _BrowserSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"no open browser session {session_id!r}")
        return session

    def _state(self, session: _BrowserSession, **extra) -> dict:
        """The verifiable result shape shared by every action: where the page is
        now, plus whatever the action adds (title / status)."""
        return {"url": session.page.url, **extra}

    def open_browser(self) -> str:
        """Launch a headless browser and return its session id. Reuse the id
        across cells and calls; the page and its cookies persist until
        `close_browser`."""
        driver = self._driver()
        browser = driver.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        session_id = uuid4().hex
        self._sessions[session_id] = _BrowserSession(browser, context, page)
        return session_id

    def goto(self, session_id: str, url: str) -> dict:
        """Navigate to `url`. Returns the final url, page title, and HTTP status."""
        session = self._session(session_id)
        resp = session.page.goto(url)
        return self._state(
            session,
            title=session.page.title(),
            status=resp.status if resp is not None else None,
        )

    def click(self, session_id: str, selector: str) -> dict:
        """Click the element matching `selector` (CSS/text). State-changing —
        gated for approval."""
        session = self._session(session_id)
        session.page.click(selector)
        return self._state(session, title=session.page.title())

    def fill(self, session_id: str, selector: str, value: str) -> dict:
        """Type `value` into the field matching `selector`. For credentials use
        `fill_secret` instead, so the value never passes through agent code."""
        session = self._session(session_id)
        session.page.fill(selector, value)
        return self._state(session)

    def fill_secret(self, session_id: str, selector: str, secret_name: str) -> dict:
        """Type a named vault secret into a field. The cleartext is resolved
        parent-side, typed into the page, and recorded so later reads mask it —
        it never reaches agent code."""
        session = self._session(session_id)
        if not self.vault:
            raise RuntimeError("no vault configured for secret injection")
        secret = self.vault.get(secret_name)
        session.page.fill(selector, secret)
        session.injected.add(secret)
        return self._state(session)

    def read_text(self, session_id: str, selector: str | None = None) -> dict:
        """Read visible text from the page (or the element matching `selector`).
        Any secret this session injected is masked before returning."""
        session = self._session(session_id)
        raw = session.page.inner_text(selector or "body")
        text = _redact(raw, session.injected)
        return {"text": truncate(text), "truncated": len(text) > MAX_OUTPUT}

    def screenshot(self, session_id: str, path: str) -> dict:
        """Save a PNG screenshot to a workspace-relative `path` (resolved
        parent-side, escape-guarded) and return where it landed. Note: a secret
        visible on screen appears in the image — it is written to disk, not
        returned to agent code."""
        session = self._session(session_id)
        target = self.ws.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        session.page.screenshot(path=str(target))
        return {"path": path}

    def close_browser(self, session_id: str) -> str:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()
        return f"closed browser session {session_id}"

    def close_all(self) -> None:
        """Tear down every open session and the Playwright driver; called from
        `Session.close()`."""
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            self._pw = None
