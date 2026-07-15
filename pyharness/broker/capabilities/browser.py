from __future__ import annotations

from importlib import import_module
from urllib.parse import urlsplit
from uuid import uuid4

from ...core.workspace import Workspace
from ...security.grants import GrantScope
from ...security.policy import ActionCategory
from ...security.profiles import ProfileStore
from ...security.sink import SecretSink
from ...security.vault import Vault
from .payload import deliver

# Page actions that change page/remote state. The default policy gates these
# behind human approval (see session.py); navigation and reads stay free. Kept
# here so the capability and the policy share one list.
MUTATING_ACTIONS = frozenset(
    {
        "browser.click",
        "browser.fill",
        "browser.fill_secret",
        "browser.select_option",
        "browser.press",
        "browser.upload",
    }
)

# Ops whose second positional arg is the element selector (used by preview() to
# describe the target). press/upload take other positional args first, so they
# rely on the ref=/selector= keyword instead.
_SELECTOR_POS_OPS = frozenset({"click", "fill", "fill_secret", "select_option"})


class _BrowserSession:
    """One live page plus the secret sink for credentials injected into it. The
    sink drives read-back redaction; the cleartext it holds never leaves the
    parent."""

    def __init__(self, browser, context, page, sink: SecretSink, profile: str | None = None):
        self.browser = browser
        self.context = context
        self.page = page
        self.sink = sink
        # The named profile this session was opened with (or saved to), or None.
        # When set, the encrypted storage_state is refreshed on close so rotated
        # cookies survive to the next session.
        self.profile = profile
        # The most recent aria snapshot's redacted text, or None. Element refs
        # (`[ref=eN]`) are only valid against this snapshot; navigation clears it
        # since a new page's DOM makes every prior ref meaningless.
        self.last_snapshot: str | None = None

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

    Secrets follow the same use-but-don't-view rule as `web.fetch`: `fill_secret`
    takes a secret *name*, resolves the cleartext parent-side, types it into the
    field, and records it on the session so every subsequent read masks it. The
    agent never sees the value, and the audit log records the name (via
    `summarize_args`), never the value.

    Every method returns a structured result (url / title / status), not a bare
    string, so the agent can check its own work — assert on status, re-read to
    confirm an effect landed."""

    name = "browser"

    def __init__(
        self,
        workspace: Workspace,
        vault: Vault | None = None,
        media=None,
        *,
        profiles: ProfileStore | None = None,
        audit=None,
    ):
        self.ws = workspace
        self.vault = vault
        # Parent-side staging for screenshots that reach the model (browser.look).
        # None means no image channel is wired (e.g. a test that never looks).
        self.media = media
        # Encrypted, named storage_state store for persistent web identity. None
        # means profiles are unavailable (no vault passphrase); open/save then
        # fail closed rather than writing cleartext.
        self.profiles = profiles
        # AuditLog for the close-time profile refresh, which does not flow through
        # the broker. None (tests, bare library use) skips that audit entry.
        self.audit = audit
        self._pw = None  # the persistent Playwright driver, started on first use
        self._sessions: dict[str, _BrowserSession] = {}

    def exports(self) -> dict:
        return {
            "open_browser": self.open_browser,
            "save_profile": self.save_profile,
            "list_profiles": self.list_profiles,
            "goto": self.goto,
            "snapshot": self.snapshot,
            "click": self.click,
            "fill": self.fill,
            "fill_secret": self.fill_secret,
            "select_option": self.select_option,
            "press": self.press,
            "scroll": self.scroll,
            "wait_for": self.wait_for,
            "upload": self.upload,
            "read_text": self.read_text,
            "look": self.look,
            "screenshot": self.screenshot,
            "close_browser": self.close_browser,
        }

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """Describe a gated page action for the approver, enriched with the page
        it will land on — context the broker can't see, since the live page is
        session state here. The url is redacted through the session's sink so a
        secret in a query string never surfaces in the confirmation.

        When the action targets an element by `ref`, a bare `[ref=e12]` is opaque,
        so we resolve it against the stored snapshot to show what it points at
        (`button "Submit"`). That line is page-controlled text, so it is repr-quoted
        and capped — a crafted accessible name cannot impersonate harness output."""
        if op == "open_browser":
            profile = kwargs.get("profile") or (args[0] if args else None)
            return ActionCategory.OUTWARD, f"open a browser logged in as profile {profile!r}{self._profile_domains(profile)}"
        if op == "save_profile":
            name = kwargs.get("name") or (args[1] if len(args) >= 2 else None)
            session = self._sessions.get(args[0] if args else kwargs.get("session_id"))
            where = f" from {session.sink.redact(session.page.url)}" if session else ""
            return ActionCategory.LOCAL, f"save this browser's login state as encrypted profile {name!r}{where}"
        session = self._sessions.get(args[0] if args else kwargs.get("session_id"))
        ref = kwargs.get("ref")
        selector = kwargs.get("selector")
        if selector is None and op in _SELECTOR_POS_OPS and len(args) >= 2:
            selector = args[1]
        if ref is not None:
            line = self._ref_line(session, ref) if session else None
            target = f"[ref={ref}] {line}" if line else f"[ref={ref}]"
        elif selector is not None:
            target = repr(selector)
        else:
            target = ""
        if op == "upload":  # name the file being staged into the page
            path = kwargs.get("path") or (args[1] if len(args) >= 2 else None)
            target = f"{target} file={path!r}".strip()
        where = f" on {session.sink.redact(session.page.url)}" if session else ""
        return ActionCategory.OUTWARD, " ".join(f"{op} {target}{where}".split())

    def scope(self, op: str, args: tuple, kwargs: dict) -> GrantScope | None:
        """The grant key for a gated page action: action-class "browser" plus the
        host of the live page. The host comes from Playwright's own `page.url`
        (harness state a page cannot forge), not from page text. `fill_secret` is
        deliberately excluded even though it mutates: releasing a credential into a
        page deserves its own look every time, so it always prompts. No session or
        no parseable host → None (not grantable → always prompts).

        `op` arrives bare (e.g. "click") while `MUTATING_ACTIONS` holds dotted
        names ("browser.click"), so the membership test re-dots it."""
        if f"browser.{op}" not in MUTATING_ACTIONS or op == "fill_secret":
            return None
        session = self._sessions.get(args[0] if args else kwargs.get("session_id"))
        if session is None:
            return None
        host = urlsplit(session.page.url).hostname
        return GrantScope("browser", host.lower()) if host else None

    def _profile_domains(self, profile) -> str:
        """A capped, parenthesized list of the cookie domains a profile would
        restore, for the open-approval prompt. Best-effort: an absent profile, no
        store, or a decrypt failure yields no suffix rather than raising — the
        approval must render even when the metadata can't."""
        if not profile or self.profiles is None:
            return ""
        try:
            domains = self.profiles.info(profile).get("domains", [])
        except Exception:  # noqa: BLE001 - preview must not fail on missing/corrupt metadata
            return ""
        if not domains:
            return ""
        shown = ", ".join(domains[:5])
        more = ", ..." if len(domains) > 5 else ""
        return f" (cookies for {shown}{more})"

    @staticmethod
    def _ref_line(session: _BrowserSession, ref: str) -> str | None:
        """The stored-snapshot line describing `ref`, repr-quoted and capped so the
        page's own text can neither forge harness output nor bloat the prompt."""
        snap = session.last_snapshot
        if not snap:
            return None
        marker = f"[ref={ref}]"
        for raw in snap.splitlines():
            if marker in raw:
                return repr(raw.strip().lstrip("- ")[:120])
        return None

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
        now, plus whatever the action adds (title / status). Injected secrets are
        masked from every string field — a secret can land in the url (a GET query
        string) or a title just as easily as in read_text, and none may
        round-trip back to agent code."""
        return session.sink.redacted({"url": session.page.url, **extra})

    def open_browser(self, profile: str | None = None) -> str:
        """Launch a headless browser and return its session id. Reuse the id
        across cells and calls; the page and its cookies persist until
        `close_browser`.

        Pass `profile="name"` to restore a saved login (see `save_profile`): the
        browser opens already authenticated for that site, skipping login + 2FA.
        Opening a profile needs approval — it acts as that identity — and the
        (rotated) state re-saves automatically when the session closes. Call
        `list_profiles()` to see what logins already exist."""
        driver = self._driver()
        browser = driver.chromium.launch(headless=True)
        if profile is not None:
            state = self._profiles().load(profile)
            context = browser.new_context(storage_state=state)
        else:
            context = browser.new_context()
        page = context.new_page()
        session_id = uuid4().hex
        self._sessions[session_id] = _BrowserSession(browser, context, page, SecretSink(self.vault), profile=profile)
        return session_id

    def save_profile(self, session_id: str, name: str) -> dict:
        """Persist this browser's login state as an encrypted profile `name`, so a
        later `open_browser(profile=name)` opens already logged in. Save it after
        you have logged in (via `fill_secret`, with any 2FA relayed through the
        conversation). The cookie material is encrypted parent-side and never
        returned to you — the result reports counts only. State-changing (it
        writes a standing credential) — gated for approval."""
        session = self._session(session_id)
        state = session.context.storage_state(indexed_db=True)
        self._profiles().save(name, state)
        session.profile = name  # so close-time refresh now keeps this profile fresh
        cookies = state.get("cookies", []) if isinstance(state, dict) else []
        origins = state.get("origins", []) if isinstance(state, dict) else []
        return {"profile": name, "cookies": len(cookies), "origins": len(origins)}

    def list_profiles(self) -> list[str]:
        """The names of saved login profiles — never any cookie values. Check this
        before logging in to a site: if a profile already exists, open with it
        instead of logging in again."""
        return self.profiles.names() if self.profiles else []

    def _profiles(self) -> ProfileStore:
        if self.profiles is None:
            raise RuntimeError(
                "profiles need a vault passphrase — set PYHARNESS_VAULT_PASSPHRASE (the CLI prompts for it)"
            )
        return self.profiles

    def _refresh_profile(self, session: _BrowserSession) -> None:
        """Re-save the (rotated) storage_state of a profile-opened session on
        close, so cookies the site rotated during the session survive. Best-effort
        — a teardown must never fail on this — and audited directly since close
        does not flow through the broker."""
        if session.profile is None or self.profiles is None:
            return
        try:
            state = session.context.storage_state(indexed_db=True)
            self.profiles.save(session.profile, state)
            if self.audit is not None:
                cookies = state.get("cookies", []) if isinstance(state, dict) else []
                origins = state.get("origins", []) if isinstance(state, dict) else []
                self.audit.record(
                    event="profile_saved",
                    profile=session.profile,
                    cookies=len(cookies),
                    origins=len(origins),
                    trigger="close",
                )
        except Exception:  # noqa: BLE001 - refresh is best-effort maintenance, never blocks teardown
            pass

    def goto(self, session_id: str, url: str) -> dict:
        """Navigate to `url`. Returns the final url, page title, and HTTP status.

        Waits for `domcontentloaded` rather than the full `load` event: a heavy
        page whose trackers and lazy media never quiesce would otherwise time out
        even though its content is already present."""
        session = self._session(session_id)
        resp = session.page.goto(url, wait_until="domcontentloaded")
        session.last_snapshot = None  # new page: every prior ref is now meaningless
        return self._state(
            session,
            title=session.page.title(),
            status=resp.status if resp is not None else None,
        )

    def snapshot(self, session_id: str, save: str | None = None) -> dict:
        """Read the page as an accessibility tree with stable element refs — the
        way to *see* what you can act on before clicking or filling.

        Returns YAML where each element carries a `[ref=eN]` handle and links show
        their url, e.g.::

            - textbox "Email" [ref=e5]
            - button "Submit application" [ref=e6]
            - link "Jobs" [ref=e7]:
              - /url: /careers

        Pass those refs to `click(ref=...)` / `fill(ref=...)` instead of guessing a
        CSS selector. Refs stay valid until you navigate (`goto` invalidates them —
        snapshot again on the new page); re-snapshot after an action that rewrites
        the page. Any secret this session injected is masked. A normal page rides
        back inline as `text`; an oversized one — or an explicit `save="path"` —
        spills to the workspace as `path`/`bytes`/`preview` with `text=None` (see
        `read_text`)."""
        session = self._session(session_id)
        raw = session.page.aria_snapshot(mode="ai")
        text = session.sink.redact(raw)
        session.last_snapshot = text  # refs and preview() resolve against this
        body = deliver(ws=self.ws, content_type="text/plain", text=text, save=save, default_name="snapshot.yaml")
        return session.sink.redacted({"url": session.page.url, "title": session.page.title(), **body})

    def _target(self, session: _BrowserSession, selector: str | None, ref: str | None) -> str:
        """Resolve an element target to a Playwright selector string, accepting a
        CSS/text `selector` or a snapshot `ref` (exactly one). A ref becomes an
        `aria-ref=` selector; it is validated against the current snapshot first so
        a stale or unknown ref fails immediately with a clear message instead of
        after Playwright's multi-second locator timeout."""
        if (selector is None) == (ref is None):
            raise ValueError("pass exactly one of selector= or ref=")
        if ref is None:
            return selector
        snap = session.last_snapshot
        if snap is None:
            raise ValueError(
                f"ref {ref!r} needs a current snapshot — call snapshot() (again after any navigation) first"
            )
        if f"[ref={ref}]" not in snap:
            raise ValueError(
                f"ref {ref!r} is not in the current snapshot — call snapshot() again after the page changed"
            )
        return f"aria-ref={ref}"

    def click(self, session_id: str, selector: str | None = None, *, ref: str | None = None) -> dict:
        """Click an element, targeted by a snapshot `ref` (preferred — take a
        `snapshot()` first) or a CSS/text `selector`. State-changing — gated for
        approval."""
        session = self._session(session_id)
        session.page.click(self._target(session, selector, ref))
        return self._state(session, title=session.page.title())

    def fill(
        self, session_id: str, selector: str | None = None, value: str | None = None, *, ref: str | None = None
    ) -> dict:
        """Type `value` into a field, targeted by a snapshot `ref` (preferred) or a
        CSS/text `selector`. For credentials use `fill_secret` instead, so the
        value never passes through agent code."""
        session = self._session(session_id)
        session.page.fill(self._target(session, selector, ref), value)
        return self._state(session)

    def fill_secret(
        self, session_id: str, selector: str | None = None, secret_name: str | None = None, *, ref: str | None = None
    ) -> dict:
        """Type a named vault secret into a field, targeted by a snapshot `ref`
        (preferred) or a CSS/text `selector`. The cleartext is resolved
        parent-side, typed into the page, and recorded so later reads mask it — it
        never reaches agent code."""
        session = self._session(session_id)
        secret = session.sink.resolve(secret_name)
        session.page.fill(self._target(session, selector, ref), secret)
        return self._state(session)

    def select_option(
        self,
        session_id: str,
        selector: str | None = None,
        *,
        ref: str | None = None,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> dict:
        """Choose an option in a `<select>` dropdown, targeted by a snapshot `ref`
        (preferred) or a CSS/text `selector`. Give exactly one of `value=` (the
        option's value attribute), `label=` (its visible text), or `index=`.
        State-changing — gated for approval."""
        session = self._session(session_id)
        target = self._target(session, selector, ref)
        chosen = {k: v for k, v in (("value", value), ("label", label), ("index", index)) if v is not None}
        session.page.select_option(target, **chosen)
        return self._state(session)

    def press(self, session_id: str, key: str, selector: str | None = None, *, ref: str | None = None) -> dict:
        """Press a key or chord (`"Enter"`, `"Tab"`, `"Control+A"`). With a `ref`/
        `selector` the key is sent to that element; otherwise to whatever is
        focused. State-changing (Enter submits a form) — gated for approval."""
        session = self._session(session_id)
        if selector is not None or ref is not None:
            session.page.press(self._target(session, selector, ref), key)
        else:
            session.page.keyboard.press(key)
        return self._state(session)

    def scroll(self, session_id: str, dy: int = 0, dx: int = 0) -> dict:
        """Scroll the viewport by `dy` (and `dx`) pixels — positive `dy` scrolls
        down. For lazy-loaded content, scroll then `snapshot` again. Free (viewport
        only, no remote effect)."""
        session = self._session(session_id)
        session.page.mouse.wheel(dx, dy)
        return self._state(session)

    def wait_for(self, session_id: str, selector: str, state: str = "visible", timeout_ms: int = 10_000) -> dict:
        """Wait until `selector` reaches `state` (`"visible"`/`"attached"`/
        `"hidden"`/`"detached"`). Returns `{... , "found": bool}` — a timeout is a
        clean `found=False`, not a traceback, so polling for an element that may
        not appear doesn't burn the turn. Free."""
        session = self._session(session_id)
        try:
            session.page.wait_for_selector(selector, state=state, timeout=timeout_ms)
            found = True
        except Exception as exc:  # noqa: BLE001 - a real Playwright TimeoutError is the "not found" signal
            if type(exc).__name__ != "TimeoutError":
                raise
            found = False
        return self._state(session, found=found)

    def upload(self, session_id: str, path: str, selector: str | None = None, *, ref: str | None = None) -> dict:
        """Attach a workspace file to a file-input, targeted by a snapshot `ref`
        (preferred) or a CSS/text `selector`. `path` is workspace-relative and
        escape-guarded. This stages the file's *contents* into the remote page (a
        page can auto-submit on change), so it is state-changing — gated, and the
        confirmation shows the filename."""
        session = self._session(session_id)
        target = self._target(session, selector, ref)
        source = self.ws.path(path)  # resolves inside the workspace; raises on escape
        session.page.set_input_files(target, str(source))
        return self._state(session, uploaded=path)

    def read_text(self, session_id: str, selector: str | None = None, save: str | None = None) -> dict:
        """Read visible text from the page (or the element matching `selector`).
        Any secret this session injected is masked before returning.

        The whole text comes back, not a capped head: a normal page rides back
        inline as `text`; a page past the inline ceiling — or an explicit
        `save="path"` — is written to the workspace and returned as
        `path`/`bytes`/`preview` with `text=None`. See `payload.deliver`."""
        session = self._session(session_id)
        raw = session.page.inner_text(selector or "body")
        text = session.sink.redact(raw)
        body = deliver(ws=self.ws, content_type="text/plain", text=text, save=save, default_name="page.txt")
        return session.sink.redacted(body)

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

    def look(self, session_id: str, full_page: bool = False) -> dict:
        """Take a screenshot and hand it to the model as an image it can actually
        *see* — for reading a chart, a rendered PDF, a layout, or a CAPTCHA the
        page shows, or confirming a visual state that text can't capture. Unlike
        `screenshot` (which writes a PNG to disk for your own tooling), `look`
        attaches the image to this call's result so the model sees it directly.

        Prefer `snapshot` for structure and reach for `look` only when you need
        pixels: the image stays in history and costs context on every later turn.
        A secret visible on screen reaches the model this way, so under the default
        policy `look` needs approval once this session has typed a secret into the
        page."""
        session = self._session(session_id)
        if self.media is None:
            raise RuntimeError("look() has no image channel in this session; use screenshot() to save to disk")
        data = session.page.screenshot(type="jpeg", quality=80, full_page=full_page)
        self.media.attach(media_type="image/jpeg", data=data)
        return self._state(session, attached=True, bytes=len(data))

    def has_injected_secrets(self, session_id: str) -> bool:
        """Whether this session has typed a vault secret into the page. The default
        policy reads this to gate `look` — a screenshot would carry that secret's
        pixels into the model's context, where text redaction can't reach."""
        session = self._sessions.get(session_id)
        return bool(session and session.sink.has_injected)

    def close_browser(self, session_id: str) -> str:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            self._refresh_profile(session)
            session.close()
        return f"closed browser session {session_id}"

    def close_all(self) -> None:
        """Tear down every open session and the Playwright driver; called from
        `Session.close()`. Profile-opened sessions re-save their state first."""
        for session in self._sessions.values():
            self._refresh_profile(session)
            session.close()
        self._sessions.clear()
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            self._pw = None
