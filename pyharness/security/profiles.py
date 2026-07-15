from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .vault import PASSPHRASE_ENV, EncryptedFile

_DEFAULT_DIR = Path.home() / ".pyharness" / "profiles"
PROFILES_DIR_ENV = "PYHARNESS_PROFILES_DIR"

# A profile name is the only agent-supplied input that becomes a filename, so it
# is validated in exactly one place (every load/save/delete/info). Lowercase
# alnum plus - and _, no dots or slashes: path traversal is structurally
# impossible, and the name is non-secret metadata like a vault secret name.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")

# Envelope schema version for the inner plaintext (distinct from EncryptedFile's
# own outer envelope version). `kind` is the seam for a future "http" cookie-jar
# profile without a format break.
_PROFILE_VERSION = 1


class ProfileStore:
    """Named, passphrase-encrypted browser ``storage_state`` blobs under one
    directory — persistent web identity that outlives a Session.

    Cookie + localStorage material is credential-grade, so it is sealed with the
    same scrypt+Fernet envelope as the secrets vault (`EncryptedFile`), one file
    per profile at ``<root>/<name>.enc``. The cleartext ``storage_state`` dict
    lives in parent-process memory only; it is never written to disk unencrypted
    and never returned to agent code (which only ever holds a profile *name*).
    """

    def __init__(self, root: str | Path, passphrase: str):
        self.root = Path(root)
        self._passphrase = passphrase

    @classmethod
    def from_env(cls) -> "ProfileStore | None":
        """Build the default store, or ``None`` when no passphrase is configured.

        Profiles hold credential-grade cookies, so a missing passphrase fails
        closed (the caller raises an instructive error) rather than falling back
        to plaintext. Unlike the vault, the directory need not pre-exist — the
        harness writes profiles, it does not pre-provision them."""
        passphrase = os.environ.get(PASSPHRASE_ENV)
        if not passphrase:
            return None
        root = Path(os.environ.get(PROFILES_DIR_ENV, _DEFAULT_DIR))
        return cls(root, passphrase)

    def _path(self, name: str) -> Path:
        if not _NAME_RE.match(name):
            raise ValueError(
                f"invalid profile name {name!r}: use lowercase letters, digits, '-' or '_' (no dots or slashes)"
            )
        return self.root / f"{name}.enc"

    def names(self) -> list[str]:
        """Every saved profile name — never any state. Reads filenames only; does
        not decrypt (so it works even against a wrong passphrase)."""
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.enc"))

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def load(self, name: str) -> dict:
        """The saved ``storage_state`` dict for `name`. Raises ``KeyError`` if the
        profile is absent (distinct from the empty dict a missing vault yields);
        a wrong passphrase surfaces as Fernet's authentication failure."""
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"no profile named {name!r}")
        envelope = EncryptedFile(path, self._passphrase).load()
        return envelope.get("state", {})

    def save(self, name: str, state: dict, *, kind: str = "browser") -> None:
        """Seal `state` (a Playwright ``storage_state`` dict) under `name`,
        wrapped with a ``kind``/timestamp header so metadata is readable without
        exposing the cookies. Atomic and 0o600 (see `EncryptedFile.save`)."""
        path = self._path(name)
        envelope = {
            "version": _PROFILE_VERSION,
            "kind": kind,
            "saved_at": time.time(),
            "state": state,
        }
        EncryptedFile(path, self._passphrase).save(envelope)

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def info(self, name: str) -> dict:
        """Non-secret metadata for the CLI / approval previews: kind, save time,
        cookie/origin counts, and the distinct cookie domains — never any cookie
        value. Decrypts the profile (values stay parent-side)."""
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"no profile named {name!r}")
        envelope = EncryptedFile(path, self._passphrase).load()
        state = envelope.get("state", {})
        cookies = state.get("cookies", [])
        origins = state.get("origins", [])
        domains = sorted({c.get("domain", "") for c in cookies if c.get("domain")})
        return {
            "kind": envelope.get("kind", "browser"),
            "saved_at": envelope.get("saved_at"),
            "cookies": len(cookies),
            "origins": len(origins),
            "domains": domains,
        }
