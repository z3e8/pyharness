"""Manage the encrypted secret file the agent draws credentials from.

    pyharness-vault set NAME [VALUE] [--host HOST ...]
                                       # value prompted (hidden) if omitted;
                                       # each --host binds injection to that host
    pyharness-vault list               # names and host bindings — never values
    pyharness-vault rm NAME

A secret set with `--host` (repeatable) can only ever be injected toward those
hosts — a request, page fill, or login aimed anywhere else is refused before it
leaves the machine. Without `--host` the secret is unbound and the approval
prompt is the check.

The file is `~/.pyharness/secrets.enc` (override with PYHARNESS_VAULT_FILE) and
is sealed with a passphrase (PYHARNESS_VAULT_PASSPHRASE, else prompted). Set the
same passphrase in the env when you run `pyharness` so the session can open it.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from ..security.vault import _DEFAULT_FILE, EncryptedFile, _entry, normalize_host


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
            entries = vault.load()
        except Exception as exc:  # wrong passphrase / corrupt file
            sys.exit(f"could not open vault: {exc}")
        for name in sorted(entries):
            _, hosts = _entry(entries[name])
            print(f"{name} -> {', '.join(hosts)}" if hosts else name)
        return

    secrets = vault.load() if vault.path.exists() else {}

    if cmd == "set":
        usage = "usage: pyharness-vault set NAME [VALUE] [--host HOST ...]"
        hosts: list[str] = []
        positional: list[str] = []
        it = iter(rest)
        for arg in it:
            if arg == "--host":
                host = next(it, None)
                if not host:
                    sys.exit(usage)
                # Store the canonical bare hostname the sink compares against, so
                # a pasted URL (`https://app.capacities.io/`) still binds to the
                # page it is meant for. Reject input with no recoverable host.
                normalized = normalize_host(host)
                if not normalized:
                    sys.exit(f"invalid --host {host!r}: no hostname")
                hosts.append(normalized)
            else:
                positional.append(arg)
        if not 1 <= len(positional) <= 2:
            sys.exit(usage)
        name = positional[0]
        value = (
            positional[1]
            if len(positional) > 1
            else getpass.getpass(f"value for {name!r}: ")
        )
        secrets[name] = {"value": value, "hosts": hosts} if hosts else value
        bound = f", bound to {', '.join(hosts)}" if hosts else ""
        vault.save(secrets)
        print(f"stored {name!r}{bound} ({len(secrets)} secret(s) in {vault.path})")
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
