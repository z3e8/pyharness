"""Manage the encrypted secret file the agent draws credentials from.

    pyharness-vault set NAME [VALUE]   # value prompted (hidden) if omitted
    pyharness-vault list               # names only — never values
    pyharness-vault rm NAME

The file is `~/.pyharness/secrets.enc` (override with PYHARNESS_VAULT_FILE) and
is sealed with a passphrase (PYHARNESS_VAULT_PASSPHRASE, else prompted). Set the
same passphrase in the env when you run `pyharness` so the session can open it.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from .security.vault import _DEFAULT_FILE, EncryptedFile


def _open() -> EncryptedFile:
    path = Path(os.environ.get("PYHARNESS_VAULT_FILE", _DEFAULT_FILE))
    passphrase = os.environ.get("PYHARNESS_VAULT_PASSPHRASE") or getpass.getpass(
        "vault passphrase: "
    )
    return EncryptedFile(path, passphrase)


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ("set", "list", "rm"):
        sys.exit(__doc__)
    cmd, rest = args[0], args[1:]
    vault = _open()

    if cmd == "list":
        try:
            for name in vault.names():
                print(name)
        except Exception as exc:  # wrong passphrase / corrupt file
            sys.exit(f"could not open vault: {exc}")
        return

    secrets = vault.load() if vault.path.exists() else {}

    if cmd == "set":
        if not rest:
            sys.exit("usage: pyharness-vault set NAME [VALUE]")
        name = rest[0]
        value = rest[1] if len(rest) > 1 else getpass.getpass(f"value for {name!r}: ")
        secrets[name] = value
        vault.save(secrets)
        print(f"stored {name!r} ({len(secrets)} secret(s) in {vault.path})")
    elif cmd == "rm":
        if not rest:
            sys.exit("usage: pyharness-vault rm NAME")
        name = rest[0]
        if name not in secrets:
            sys.exit(f"no secret named {name!r}")
        del secrets[name]
        vault.save(secrets)
        print(f"removed {name!r} ({len(secrets)} secret(s) remaining)")


if __name__ == "__main__":
    main()
