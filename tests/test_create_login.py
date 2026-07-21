"""Agent-minted site logins: the parent generates the password, stores it (and
the per-site plus-address) host-bound in the vault, and the agent only ever
holds the vault names — the password value is never choosable or readable from
agent code."""

from __future__ import annotations

import string

import pytest

from pyharness import Vault
from pyharness.security.passwords import DEFAULT_SYMBOLS, generate_password
from pyharness.security.sink import SecretSink
from pyharness.security.vault import EncryptedFile


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


def _file_vault(tmp_path) -> Vault:
    return Vault(file=EncryptedFile(tmp_path / "secrets.enc", "pw"))


def test_vault_store_writes_host_bound_entry(tmp_path):
    vault = _file_vault(tmp_path)
    vault.store("gh", "tok", hosts=("https://API.GitHub.com/",))
    # Normalized on write, resolvable immediately and after a fresh open.
    assert vault.get("gh") == "tok"
    assert vault.hosts("gh") == ("api.github.com",)
    reopened = _file_vault(tmp_path)
    assert reopened.get("gh") == "tok"
    assert reopened.hosts("gh") == ("api.github.com",)
    # No hosts -> stored unbound.
    vault.store("free", "v")
    assert _file_vault(tmp_path).hosts("free") is None


def test_vault_store_refuses_overwrite_across_backends(tmp_path, monkeypatch):
    vault = Vault({"dictname": "v"}, file=EncryptedFile(tmp_path / "secrets.enc", "pw"))
    monkeypatch.setenv("PYHARNESS_SECRET_ENVNAME", "v")
    vault.store("filename", "v")
    for name in ("dictname", "envname", "filename"):
        with pytest.raises(ValueError):
            vault.store(name, "other")
    # store_many is atomic: one duplicate rejects the whole batch.
    with pytest.raises(ValueError):
        vault.store_many({"fresh": ("v", None), "filename": ("v", None)})
    assert "fresh" not in vault.names()


def test_vault_store_without_file_backend_raises():
    with pytest.raises(RuntimeError, match="PYHARNESS_VAULT_PASSPHRASE"):
        Vault({"x": "y"}).store("name", "value")


def test_vault_store_updates_read_cache(tmp_path):
    vault = _file_vault(tmp_path)
    assert vault.names() == []  # primes the file cache
    vault.store("new", "v", hosts=("example.com",))
    # The same instance (shared with every SecretSink) sees the entry at once.
    assert vault.names() == ["new"]
    assert SecretSink(vault).resolve("new", target_host="example.com") == "v"


def test_cli_get_reveals_value_human_only(tmp_path, monkeypatch, capsys):
    from pyharness.cli import vault as cli_vault

    monkeypatch.setenv("PYHARNESS_VAULT_FILE", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "pw")
    monkeypatch.setattr(
        "sys.argv", ["pyharness-vault", "set", "gh", "tok", "--host", "github.com"]
    )
    cli_vault.main()
    capsys.readouterr()
    monkeypatch.setattr("sys.argv", ["pyharness-vault", "get", "gh"])
    cli_vault.main()
    # Value alone on stdout (unwrapped from the hosts dict) so it pipes cleanly.
    assert capsys.readouterr().out == "tok\n"
    monkeypatch.setattr("sys.argv", ["pyharness-vault", "get", "missing"])
    with pytest.raises(SystemExit, match="no secret named"):
        cli_vault.main()
    monkeypatch.setattr("sys.argv", ["pyharness-vault", "get"])
    with pytest.raises(SystemExit, match="usage"):
        cli_vault.main()


def test_from_env_attaches_backend_before_file_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("PYHARNESS_VAULT_FILE", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "pw")
    vault = Vault.from_env()
    assert vault.names() == []  # missing file reads as empty, not an error
    vault.store("first", "v")
    assert (tmp_path / "secrets.enc").exists()
    assert _file_vault(tmp_path).get("first") == "v"
    # Without a passphrase there is no backend and store fails closed.
    monkeypatch.delenv("PYHARNESS_VAULT_PASSPHRASE")
    with pytest.raises(RuntimeError):
        Vault.from_env().store("x", "y")
