from __future__ import annotations

from .vault import Vault


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
    """

    def __init__(self, vault: Vault | None):
        self._vault = vault
        self._injected: set[str] = set()

    @property
    def has_injected(self) -> bool:
        """Whether this sink has resolved any secret — i.e. a live credential is
        present in its injection context. The default policy reads this to gate a
        screenshot-to-model (`browser.look`) once a secret was typed into the page,
        since pixels can't be masked the way text can."""
        return bool(self._injected)

    def resolve(self, name: str) -> str:
        """Resolve a secret name to cleartext parent-side and record it for later
        masking. Raises if no vault is configured to inject from."""
        if self._vault is None:
            raise RuntimeError("no vault configured for secret injection")
        secret = self._vault.get(name)
        self._injected.add(secret)
        return secret

    def track(self, value: str) -> None:
        """Record a cleartext *derived* from a vault secret parent-side (a TOTP
        code from a stored seed) for the same masking as a resolved secret — the
        page may echo the derived value even though it was never a vault entry."""
        self._injected.add(value)

    def redact(self, text: str) -> str:
        """Mask every cleartext this sink has resolved out of `text`. Only values
        this sink injected are masked — no need to scan for arbitrary secrets."""
        for secret in self._injected:
            if secret:
                text = text.replace(secret, "***")
        return text

    def redact_bytes(self, data: bytes) -> bytes:
        """Mask every resolved cleartext out of a raw byte body before it is
        written to disk. The binary counterpart of `redact`: a secret echoed into
        a saved payload must not survive to the workspace file any more than it may
        round-trip through returned text."""
        for secret in self._injected:
            if secret:
                data = data.replace(secret.encode(), b"***")
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
