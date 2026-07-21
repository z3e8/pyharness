from __future__ import annotations

from ...security.passwords import generate_password
from ...security.policy import ActionCategory
from ...security.vault import Vault, normalize_host


def derive_email(base_email: str, host: str) -> str:
    """The per-site plus-address for `host`: `local+<host>@domain`. The full
    normalized hostname is the tag (dots are legal in a local part) — it is
    deterministic and collision-free without a public-suffix list. The base must
    be a plain address: a pre-tagged base would produce a double tag."""
    local, sep, domain = base_email.strip().partition("@")
    if not sep or not local or not domain or "@" in domain:
        raise ValueError(f"identity email {base_email!r} is not a plain address")
    if "+" in local:
        raise ValueError(
            f"identity email {base_email!r} already carries a +tag; "
            "set the bare base address"
        )
    return f"{local}+{host}@{domain}"


def entry_names(host: str) -> tuple[str, str]:
    """The `(email, password)` vault names for `host`. Underscore slug, because
    the vault's env fallback maps a name to `PYHARNESS_SECRET_<NAME.upper()>`
    and dots/dashes are invalid there."""
    slug = host.replace(".", "_").replace("-", "_")
    return f"{slug}_email", f"{slug}_password"


class SecretsCapability:
    """Lets the agent discover which credentials it can reference — names only —
    and mint a new site login without ever holding the password.

    The values never leave the parent: `secrets()` returns the list of available
    secret names; the agent passes one of these names to a capability's auth
    argument (e.g. web.fetch(url, auth="github")) and the broker injects the
    cleartext parent-side at the point of use. `create_login` generates and
    stores a credential the same way — the agent only ever gets the names back."""

    name = "vault"

    def __init__(self, vault: Vault, *, identity_email: str | None = None):
        self.vault = vault
        self.identity_email = identity_email

    def exports(self) -> dict:
        return {"secrets": self.list_names, "create_login": self.create_login}

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """Describe a create_login for the approver (`secrets` is never gated).
        LOCAL: the call itself only writes two vault entries — submitting the
        signup form is separately gated per fill/click. Built from structured
        args + config, never from page text; must not raise on bad input (the
        op body reports that after approval)."""
        host = normalize_host(str(args[0] if args else kwargs.get("site", "")))
        email_name, password_name = entry_names(host)
        try:
            email = derive_email(self.identity_email or "", host)
        except ValueError:
            email = "(PYHARNESS_IDENTITY_EMAIL unset or invalid)"
        existing = set(self.vault.names())
        if email_name in existing and password_name in existing:
            return (
                ActionCategory.LOCAL,
                f"reuse the existing login entries for {host} "
                f"({email_name} / {password_name})",
            )
        length = kwargs.get("length", 20)
        return (
            ActionCategory.LOCAL,
            f"create a login for {host}: email {email}, new {length}-char password "
            f"-> vault entries {email_name} / {password_name} (bound to {host})",
        )

    def scope(self, op: str, args: tuple, kwargs: dict) -> None:
        # Minting an identity is never grant-covered (like fill_secret/fill_totp):
        # every site deserves its own look, so create_login always prompts.
        return None

    def list_names(self) -> list[str]:
        """Return the NAMES of secrets available to inject (never the values).
        Reference one by name in a capability's `auth` argument."""
        return self.vault.names()

    def create_login(
        self, site: str, *, length: int = 20, symbols: bool | str = True
    ) -> dict:
        """Mint a signup identity for `site` (a URL or hostname): derive a
        per-site email from the user's configured address and generate a strong
        password, storing both in the vault bound to the site's host. Needs
        human approval.

        Returns {"host", "email", "email_secret", "password_secret", "created",
        "password_length"}. Type the email into the form with fill(); type the
        password ONLY with fill_secret(session_id, ref=..., secret=password_secret)
        — its value is not obtainable, by design. `length` (12-64) and `symbols`
        (True / False / a string of allowed punctuation) exist to satisfy site
        password policies. If the login already exists the same dict comes back
        with created=False; existing entries are never overwritten.
        """
        host = normalize_host(site)
        if not host:
            raise ValueError(f"no hostname in {site!r}")
        if not self.identity_email:
            raise RuntimeError(
                "set PYHARNESS_IDENTITY_EMAIL in .env to enable create_login — "
                "it is the base address per-site signup emails derive from"
            )
        email_name, password_name = entry_names(host)
        existing = set(self.vault.names())
        have = {n for n in (email_name, password_name) if n in existing}
        if have == {email_name, password_name}:
            # Idempotent: a retried signup recovers the recorded identity — the
            # stored email is not secret (it is returned in clear on creation).
            return {
                "host": host,
                "email": self.vault.get(email_name),
                "email_secret": email_name,
                "password_secret": password_name,
                "created": False,
                "password_length": len(self.vault.get(password_name)),
            }
        if have:
            raise RuntimeError(
                f"vault entry {have.pop()!r} exists without its partner — "
                "resolve with pyharness-vault (rm or set) before create_login"
            )
        email = derive_email(self.identity_email, host)
        password = generate_password(length, symbols)
        # One write for the pair: a crash cannot leave half a login behind.
        self.vault.store_many(
            {email_name: (email, (host,)), password_name: (password, (host,))}
        )
        return {
            "host": host,
            "email": email,
            "email_secret": email_name,
            "password_secret": password_name,
            "created": True,
            "password_length": length,
        }
