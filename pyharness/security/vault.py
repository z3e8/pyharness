from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from ..util import ensure_private_dir

# scrypt KDF parameters (interactive-login class; ~tens of ms to derive).
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_DEFAULT_FILE = Path.home() / ".pyharness" / "secrets.enc"

# Environment that carries cleartext secrets and must never reach agent code.
DEFAULT_ENV_PREFIX = "PYHARNESS_SECRET_"
PASSPHRASE_ENV = "PYHARNESS_VAULT_PASSPHRASE"


def normalize_host(raw: str) -> str:
    """Canonicalize a host binding to the bare lowercase hostname the SecretSink
    compares against — every capability derives its target host as
    `urlsplit(url).hostname`, so a binding must reduce to the same form or it can
    never match. Accepts a full URL (`https://app.capacities.io/`), a `host:port`,
    or a bare host, so a secret bound with a pasted URL still matches its page.
    Returns "" for input with no recoverable host (caller drops it)."""
    text = str(raw).strip().lower()
    if not text:
        return ""
    # urlsplit only fills .hostname when a `//` authority is present; add one for
    # a bare host so `app.capacities.io` and `app.capacities.io/path` both reduce.
    if "//" not in text:
        text = "//" + text
    return urlsplit(text).hostname or ""


def _entry(raw) -> tuple[str, tuple[str, ...] | None]:
    """Unpack a stored entry. A bare string is an unbound secret (injectable
    toward any host the human approves); a `{"value": ..., "hosts": [...]}` dict
    binds the secret to those hosts. Hosts are normalized to a bare lowercase
    hostname (see `normalize_host`) so a binding stored as a URL still matches;
    an empty host list folds to unbound."""
    if isinstance(raw, dict):
        hosts = tuple(
            sorted({normalize_host(h) for h in raw.get("hosts") or ()} - {""})
        )
        return raw["value"], hosts or None
    return raw, None


class EncryptedFile:
    """A passphrase-encrypted JSON map of secret name -> cleartext value.

    The key is derived from the passphrase with scrypt (stdlib); the plaintext
    map is sealed with Fernet (AES-128-CBC + HMAC, from `cryptography`). The file
    is a JSON envelope so it stays portable and the KDF salt/params travel with
    it — only the passphrase is needed to open it elsewhere. A wrong passphrase
    fails to decrypt rather than returning garbage (Fernet is authenticated)."""

    def __init__(self, path: str | Path, passphrase: str):
        self.path = Path(path)
        self._passphrase = passphrase

    def _fernet(self, salt: bytes):
        from cryptography.fernet import Fernet

        key = hashlib.scrypt(
            self._passphrase.encode(),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=32,
        )
        return Fernet(base64.urlsafe_b64encode(key))

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        envelope = json.loads(self.path.read_text())
        salt = base64.b64decode(envelope["salt"])
        plaintext = self._fernet(salt).decrypt(envelope["ciphertext"].encode())
        return json.loads(plaintext)

    def save(self, secrets: dict) -> None:
        salt = os.urandom(16)
        ciphertext = self._fernet(salt).encrypt(json.dumps(secrets).encode())
        envelope = {
            "version": 1,
            "kdf": "scrypt",
            "salt": base64.b64encode(salt).decode(),
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
            "ciphertext": ciphertext.decode(),
        }
        # Write to a sibling temp file created 0o600, then atomically replace: a
        # crash mid-write leaves the previous file intact, and the plaintext-key
        # material is never briefly world-readable under a permissive umask.
        ensure_private_dir(
            self.path.parent
        )  # ~/.pyharness (0700) — holds vault + profiles
        fd, tmp = tempfile.mkstemp(
            dir=self.path.parent, prefix=self.path.name, suffix=".tmp"
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(envelope, indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def names(self) -> list[str]:
        return sorted(self.load())


class Vault:
    """Secret store with one hard rule: no capability exposed to agent code ever
    returns a secret's cleartext.

    The agent references a secret by name; the broker/tools resolve it here, in
    the parent, and inject it at the point of use (see web.fetch). `get` is
    deliberately NOT placed in the agent's kernel namespace — only `names()` is
    exposed (via the vault capability), and that returns names, never values.

    Resolution order, first hit wins: in-memory dict, then environment
    (PYHARNESS_SECRET_<NAME>), then the encrypted file backend. Swap any backend
    for 1Password / Bitwarden / Hashicorp later behind this same interface.
    """

    def __init__(
        self,
        secrets: dict[str, str] | None = None,
        env_prefix: str = DEFAULT_ENV_PREFIX,
        file: EncryptedFile | None = None,
    ):
        self._secrets = dict(secrets or {})
        self._env_prefix = env_prefix
        self._file = file
        self._file_cache: dict[str, str] | None = None

    @property
    def env_prefix(self) -> str:
        """The prefix under which env-backed secrets live. Agent-controlled code
        must never see these variables (see broker/remote env scrubbing)."""
        return self._env_prefix

    @classmethod
    def from_env(cls, *, env_prefix: str = DEFAULT_ENV_PREFIX) -> Vault:
        """Build the default vault. Attaches the encrypted-file backend whenever
        a passphrase (PYHARNESS_VAULT_PASSPHRASE) is set — the file itself need
        not exist yet: reads of a missing file yield no secrets, and the first
        `store` creates it (so `create_login` works before any `pyharness-vault
        set`). Without a passphrase, dict + env only."""
        passphrase = os.environ.get(PASSPHRASE_ENV)
        path = Path(os.environ.get("PYHARNESS_VAULT_FILE", _DEFAULT_FILE))
        file = EncryptedFile(path, passphrase) if passphrase else None
        return cls(env_prefix=env_prefix, file=file)

    def _file_secrets(self) -> dict[str, str]:
        if self._file_cache is None:
            self._file_cache = self._file.load() if self._file else {}
        return self._file_cache

    def _lookup(self, name: str):
        """The raw stored entry for `name` — a bare string, or a
        `{"value", "hosts"}` dict for a host-bound secret (see `_entry`)."""
        if name in self._secrets:
            return self._secrets[name]
        env = os.environ.get(self._env_prefix + name.upper())
        if env is not None:
            return env
        file_secrets = self._file_secrets()
        if name in file_secrets:
            return file_secrets[name]
        raise KeyError(f"secret {name!r} not found")

    def get(self, name: str) -> str:
        return _entry(self._lookup(name))[0]

    def hosts(self, name: str) -> tuple[str, ...] | None:
        """The hosts `name` is bound to, or None for an unbound secret. A bound
        secret may only be injected toward one of these hosts — the `SecretSink`
        enforces it structurally, before any request leaves the parent."""
        return _entry(self._lookup(name))[1]

    def names(self) -> list[str]:
        """Every secret name available to inject — never the values. Env-derived
        names are lowercased to match how `get` upper-cases the lookup."""
        names = set(self._secrets)
        for key in os.environ:
            if key.startswith(self._env_prefix):
                names.add(key[len(self._env_prefix) :].lower())
        names.update(self._file_secrets())
        return sorted(names)

    def store(
        self, name: str, value: str, *, hosts: tuple[str, ...] | None = None
    ) -> None:
        """Write one entry to the encrypted-file backend. Refuses to overwrite a
        name resolvable from ANY backend — rotation is a human act (the
        `pyharness-vault` CLI); nothing agent-reachable can replace a credential."""
        self.store_many({name: (value, hosts)})

    def store_many(self, items: dict[str, tuple[str, tuple[str, ...] | None]]) -> None:
        """Write several entries in one load/save cycle (`create_login` stores an
        email+password pair this way, so a crash cannot leave half a login).
        Same overwrite refusal as `store`; not exposed to agent code — callers
        are parent-side capabilities and tests only.

        Loads fresh from disk (not the read cache) before mutating, so a
        `pyharness-vault set` done mid-session in another terminal is not
        clobbered; the cache is then updated so already-built `SecretSink`s
        resolve the new names immediately."""
        if self._file is None:
            raise RuntimeError(
                "the vault file is not available — set PYHARNESS_VAULT_PASSPHRASE "
                "(and optionally PYHARNESS_VAULT_FILE) so new credentials can be stored"
            )
        existing = set(self.names())
        for name in items:
            if name in existing:
                raise ValueError(
                    f"secret {name!r} already exists — refusing to overwrite; "
                    "manage it with pyharness-vault"
                )
        entries = self._file.load()
        for name, (value, hosts) in items.items():
            bound = tuple(sorted({normalize_host(h) for h in hosts or ()} - {""}))
            entries[name] = {"value": value, "hosts": list(bound)} if bound else value
        self._file.save(entries)
        self._file_cache = entries
