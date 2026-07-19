from __future__ import annotations

from urllib.parse import quote, quote_plus

from .vault import Vault


def _mask_forms(value: str) -> set[str]:
    """Every literal spelling of `value` that could appear in agent-visible output.
    A query-style secret survives into `resp.url` percent-encoded (`p@ss` ->
    `p%40ss`), so a raw-substring mask would miss it; include the URL-encoded forms
    so redaction still catches it. Empty and unchanged forms fold together."""
    forms = {value, quote(value, safe=""), quote_plus(value)}
    forms.discard("")
    return forms


class SecretSink:
    """The one place a named vault secret becomes cleartext for injection.

    A capability that pushes a credential into an outbound sink — an HTTP header,
    a query param, a request body field, a browser input — resolves it through a
    sink rather than touching the vault directly. One sink is scoped to one
    injection context (a browser session, a single HTTP request), so it knows
    exactly which cleartexts it has handed out and can mask them back out of
    anything the agent later reads: a response body, page text, a redirect url
    that echoed a query-string secret. A resolved secret must never round-trip
    through agent-visible output.

    Scope is deliberately narrow: resolve-and-track for injection, not general
    sealing or encryption. The cleartext lives only here in the parent; the agent
    holds a name, and the audit log records that name (via `summarize_args`),
    never the value.

    **Masking is defense-in-depth, not a guarantee.** `redact` is a literal
    substring replace over a small set of spellings — the raw value plus its
    URL-quoted forms (see `_mask_forms`). It catches the common case (a secret
    echoed verbatim, or percent-encoded into a redirect URL) but *not* a value a
    server re-encodes before echoing it: base64/hex, HTML entities, JSON/unicode
    escapes, or a value split across chunks all pass through unmasked. The real
    containment is that a resolved secret only ever travels to a vetted host
    (host-binding in `resolve`) and never enters agent-visible output by design;
    masking is the backstop for the incidental echo, deliberately not relied on
    as the boundary. Widening the encodings covered would trade a few more catches
    for false-positive `***` noise on innocent text, so it is intentionally not
    done here.
    """

    def __init__(self, vault: Vault | None):
        self._vault = vault
        # Every literal spelling to mask out of agent-visible output — the raw
        # cleartext plus its URL-encoded forms (see _mask_forms).
        self._masks: set[str] = set()

    @property
    def has_injected(self) -> bool:
        """Whether this sink has resolved any secret — i.e. a live credential is
        present in its injection context. The default policy reads this to gate a
        screenshot once a secret was typed into the page, since pixels can't be
        masked the way text can."""
        return bool(self._masks)

    def resolve(self, name: str, *, target_host: str | None = None) -> str:
        """Resolve a secret name to cleartext parent-side and record it for later
        masking. Raises if no vault is configured to inject from.

        `target_host` is the host the injected secret is about to travel to (the
        request URL's host, the browser page's host, the IMAP server). A secret
        bound to hosts at vault-config time (`pyharness-vault set NAME --host H`)
        is refused toward any other host — structurally, before anything goes out
        — so a look-alike destination is impossible, not merely visible in the
        approval prompt. Unbound secrets keep the approval gate as their check."""
        if self._vault is None:
            raise RuntimeError("no vault configured for secret injection")
        hosts = self._vault.hosts(name)
        if hosts is not None:
            if not target_host:
                raise PermissionError(
                    f"secret {name!r} is bound to host(s) {', '.join(hosts)} and "
                    "cannot be injected into a context with no target host"
                )
            if target_host.lower() not in hosts:
                raise PermissionError(
                    f"secret {name!r} is bound to host(s) {', '.join(hosts)}; "
                    f"refusing to send it to {target_host!r}"
                )
        secret = self._vault.get(name)
        self._masks |= _mask_forms(secret)
        return secret

    def track(self, value: str) -> None:
        """Record a cleartext *derived* from a vault secret parent-side (a TOTP
        code from a stored seed) for the same masking as a resolved secret — the
        page may echo the derived value even though it was never a vault entry."""
        self._masks |= _mask_forms(value)

    def redact(self, text: str) -> str:
        """Mask every cleartext this sink has resolved out of `text`. Only values
        this sink injected are masked — no need to scan for arbitrary secrets."""
        for mask in self._masks:
            text = text.replace(mask, "***")
        return text

    def redact_bytes(self, data: bytes) -> bytes:
        """Mask every resolved cleartext out of a raw byte body before it is
        written to disk. The binary counterpart of `redact`: a secret echoed into
        a saved payload must not survive to the workspace file any more than it may
        round-trip through returned text."""
        for mask in self._masks:
            data = data.replace(mask.encode(), b"***")
        return data

    def redacted(self, value):
        """Redact `value` wherever a string can hide a resolved secret: a bare
        string, or the string leaves of a nested mapping or list such as an HTTP
        result, its headers, and its parsed links/forms. Non-string leaves pass
        through."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: self.redacted(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redacted(item) for item in value]
        return value
