"""Manage the encrypted browser login profiles the agent opens with.

    pyharness-profiles list                 # names + metadata — never cookie values
    pyharness-profiles rm NAME
    pyharness-profiles login NAME [URL]     # headed browser; log in, press Enter to save

A profile stores a site's login state (cookies + localStorage) so the agent can
`open_browser(profile="NAME")` already authenticated, skipping login + 2FA. Files
live under `~/.pyharness/profiles/` (override `PYHARNESS_PROFILES_DIR`) and are
sealed with the vault passphrase (`PYHARNESS_VAULT_PASSPHRASE`, else prompted) —
the same one you set when you run `pyharness`.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import os
import sys

from ..security.profiles import PROFILES_DIR_ENV, _DEFAULT_DIR, ProfileStore


def _open() -> ProfileStore:
    passphrase = os.environ.get("PYHARNESS_VAULT_PASSPHRASE") or getpass.getpass("vault passphrase: ")
    root = os.environ.get(PROFILES_DIR_ENV, _DEFAULT_DIR)
    return ProfileStore(root, passphrase)


def _capture(store: ProfileStore, name: str, url: str | None) -> None:
    """Open a headed chromium, let the human log in, then encrypt the state."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "profiles login needs the browser extra: install 'pyharness[browser]' and run 'playwright install chromium'"
        )
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        if url:
            page.goto(url)
        input(f"Log in to {name!r} in the browser window, then press Enter here to save... ")
        state = context.storage_state(indexed_db=True)
        browser.close()
    store.save(name, state)
    cookies = state.get("cookies", []) if isinstance(state, dict) else []
    print(f"saved profile {name!r} ({len(cookies)} cookie(s)) to {store.root}")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ("list", "rm", "login"):
        sys.exit(__doc__)
    cmd, rest = args[0], args[1:]
    store = _open()

    if cmd == "list":
        names = store.names()
        if not names:
            print("(no profiles)")
            return
        for name in names:
            try:
                info = store.info(name)
            except Exception as exc:  # wrong passphrase / corrupt file
                print(f"{name}\t(could not read: {exc})")
                continue
            saved = info.get("saved_at")
            when = _dt.datetime.fromtimestamp(saved).strftime("%Y-%m-%d %H:%M") if saved else "?"
            domains = ", ".join(info.get("domains", [])[:5]) or "-"
            print(f"{name}\t{when}\t{info['cookies']} cookie(s)\t{domains}")
        return

    if cmd == "rm":
        if not rest:
            sys.exit("usage: pyharness-profiles rm NAME")
        name = rest[0]
        if not store.exists(name):
            sys.exit(f"no profile named {name!r}")
        store.delete(name)
        print(f"removed {name!r}")
        return

    if cmd == "login":
        if not rest:
            sys.exit("usage: pyharness-profiles login NAME [URL]")
        _capture(store, rest[0], rest[1] if len(rest) > 1 else None)


if __name__ == "__main__":
    main()
