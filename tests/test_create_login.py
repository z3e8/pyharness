"""Agent-minted site logins: the parent generates the password, stores it (and
the per-site plus-address) host-bound in the vault, and the agent only ever
holds the vault names — the password value is never choosable or readable from
agent code."""

from __future__ import annotations

import string

import pytest

from pyharness.security.passwords import DEFAULT_SYMBOLS, generate_password


def test_generate_password_length_and_class_coverage():
    for length in (12, 20, 64):
        pw = generate_password(length)
        assert len(pw) == length
        assert any(c in string.ascii_lowercase for c in pw)
        assert any(c in string.ascii_uppercase for c in pw)
        assert any(c in string.digits for c in pw)
        assert any(c in DEFAULT_SYMBOLS for c in pw)
        assert set(pw) <= set(string.ascii_letters + string.digits + DEFAULT_SYMBOLS)


def test_generate_password_symbols_off_and_custom_set():
    pw = generate_password(20, symbols=False)
    assert set(pw) <= set(string.ascii_letters + string.digits)
    # Still covers the three remaining classes.
    assert any(c in string.ascii_lowercase for c in pw)
    assert any(c in string.ascii_uppercase for c in pw)
    assert any(c in string.digits for c in pw)
    # A custom set restricts punctuation to exactly those characters and
    # guarantees at least one of them.
    pw = generate_password(20, symbols="-_")
    assert set(pw) <= set(string.ascii_letters + string.digits + "-_")
    assert any(c in "-_" for c in pw)


def test_generate_password_rejects_weak_or_bad_requests():
    with pytest.raises(ValueError):
        generate_password(11)  # the agent cannot force a weak password
    with pytest.raises(ValueError):
        generate_password(65)
    with pytest.raises(ValueError):
        generate_password(20, symbols="")  # empty custom set
    with pytest.raises(ValueError):
        generate_password(20, symbols="ab")  # letters are not symbols


def test_generate_password_is_random():
    assert generate_password(20) != generate_password(20)
