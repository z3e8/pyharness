"""Parent-side password generation for agent-minted logins.

The agent never chooses, supplies, or sees a password: `create_login` (the only
caller in the capability layer) invokes this in the parent and the value goes
straight into the vault. The knobs exposed to the agent (length, symbol set)
exist to satisfy site password policies — the floors here are what keep the
agent from talking a site *and* the harness into a weak credential.
"""

from __future__ import annotations

import secrets
import string

DEFAULT_SYMBOLS = "!@#$%^&*()-_=+?.,"
MIN_LENGTH = 12
MAX_LENGTH = 64


def generate_password(length: int = 20, symbols: bool | str = True) -> str:
    """A cryptographically random password with at least one character from
    each active class (lower, upper, digit, and — unless disabled — symbol).

    `symbols` is True for the default symbol set, False for alphanumeric only
    (sites that reject punctuation), or a string of exactly the punctuation a
    picky site allows. Length is clamped by refusal, not silently: out-of-bounds
    raises so the caller learns the harness's floor instead of getting a
    different password than requested.
    """
    if not MIN_LENGTH <= length <= MAX_LENGTH:
        raise ValueError(
            f"length must be between {MIN_LENGTH} and {MAX_LENGTH}, got {length}"
        )
    if isinstance(symbols, str):
        if not symbols or not set(symbols) <= set(string.punctuation):
            raise ValueError(
                "symbols must be a non-empty string of ASCII punctuation "
                f"characters, got {symbols!r}"
            )
        symbol_set = symbols
    else:
        symbol_set = DEFAULT_SYMBOLS if symbols else ""
    classes = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
    if symbol_set:
        classes.append(symbol_set)
    # Constructive class coverage: one draw per class, the rest from the union,
    # then shuffle — no rejection loop, uniform beyond the coverage guarantee.
    alphabet = "".join(classes)
    chars = [secrets.choice(c) for c in classes]
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
