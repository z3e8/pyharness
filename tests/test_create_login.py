"""Agent-minted site logins: the parent generates the password, stores it (and
the per-site plus-address) host-bound in the vault, and the agent only ever
holds the vault names — the password value is never choosable or readable from
agent code."""

from __future__ import annotations

import string

import pytest

from pyharness import Budget, Policy, Vault
from pyharness.audit import AuditLog
from pyharness.broker import Broker
from pyharness.broker.capabilities import SecretsCapability
from pyharness.broker.capabilities.secrets import derive_email, entry_names
from pyharness.broker.dispatch import ApprovalOutcome, PermissionDenied
from pyharness.security.passwords import DEFAULT_SYMBOLS, generate_password
from pyharness.security.policy import ActionCategory
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


def test_derive_email():
    assert (
        derive_email("a.b@example.com", "app.example.io")
        == "a.b+app.example.io@example.com"
    )
    with pytest.raises(ValueError):
        derive_email("a+tag@example.com", "h.io")  # pre-tagged base -> double tag
    for bad in ("not-an-address", "@example.com", "a@", "a@b@c"):
        with pytest.raises(ValueError):
            derive_email(bad, "h.io")


def test_entry_names_slug_survives_env_fallback(monkeypatch):
    assert entry_names("app.example.com") == (
        "app_example_com_email",
        "app_example_com_password",
    )
    # The slug round-trips through the PYHARNESS_SECRET_<NAME> env mapping.
    monkeypatch.setenv("PYHARNESS_SECRET_MY_SITE_COM_PASSWORD", "v")
    _, password_name = entry_names("my-site.com")
    assert Vault().get(password_name) == "v"


def _capability(tmp_path, email="me@example.com"):
    vault = _file_vault(tmp_path)
    return SecretsCapability(vault, identity_email=email), vault


def test_create_login_mints_host_bound_pair(tmp_path):
    cap, vault = _capability(tmp_path)
    result = cap.create_login("https://app.example.com/signup")
    assert result == {
        "host": "app.example.com",
        "email": "me+app.example.com@example.com",
        "email_secret": "app_example_com_email",
        "password_secret": "app_example_com_password",
        "created": True,
        "password_length": 20,
    }
    # Both entries stored, bound to the site's host; the password is strong and
    # only reachable parent-side.
    assert vault.get("app_example_com_email") == result["email"]
    assert vault.hosts("app_example_com_email") == ("app.example.com",)
    assert vault.hosts("app_example_com_password") == ("app.example.com",)
    password = vault.get("app_example_com_password")
    assert len(password) == 20
    assert password not in repr(result)
    # The stored password resolves toward its own host and nowhere else.
    sink = SecretSink(vault)
    assert (
        sink.resolve("app_example_com_password", target_host="app.example.com")
        == password
    )
    with pytest.raises(PermissionError):
        sink.resolve(
            "app_example_com_password", target_host="app.example.com.evil.example"
        )
    # And the sink now masks it out of anything read back.
    assert password not in sink.redact(f"the page echoed {password}")
    # The agent-facing listing shows the new names.
    assert set(cap.list_names()) == {
        "app_example_com_email",
        "app_example_com_password",
    }


def test_create_login_is_idempotent_and_never_overwrites(tmp_path):
    cap, vault = _capability(tmp_path)
    first = cap.create_login("app.example.com", length=14)
    password = vault.get("app_example_com_password")
    again = cap.create_login("app.example.com")
    assert again == {**first, "created": False, "password_length": 14}
    assert vault.get("app_example_com_password") == password  # unchanged


def test_create_login_rejects_partial_state_and_bad_setup(tmp_path):
    cap, vault = _capability(tmp_path)
    vault.store("app_example_com_email", "stale@example.com")
    with pytest.raises(RuntimeError, match="app_example_com_email"):
        cap.create_login("app.example.com")
    with pytest.raises(ValueError):
        cap.create_login("https:///")  # no hostname
    with pytest.raises(ValueError):
        cap.create_login("ok.example.com", length=8)  # below the floor
    no_email, _ = _capability(tmp_path, email=None)
    with pytest.raises(RuntimeError, match="PYHARNESS_IDENTITY_EMAIL"):
        no_email.create_login("ok.example.com")
    no_backend = SecretsCapability(Vault(), identity_email="me@example.com")
    with pytest.raises(RuntimeError, match="PYHARNESS_VAULT_PASSPHRASE"):
        no_backend.create_login("ok.example.com")


class _Approver:
    def __init__(self, outcome=ApprovalOutcome.ONCE):
        self.outcome = outcome
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.outcome


def test_create_login_gating_through_broker(tmp_path):
    approver = _Approver()
    broker = Broker(
        Policy(require_approval={"vault.create_login"}),
        AuditLog(tmp_path / "audit.jsonl"),
        Budget(),
        approver=approver,
    )
    cap, vault = _capability(tmp_path)
    broker.register(cap)
    result = broker.call("vault", "create_login", "app.example.com")
    assert result["created"] is True
    (request,) = approver.requests
    assert request.category is ActionCategory.LOCAL
    # Never grant-covered: minting an identity prompts anew for every site.
    assert request.scope is None
    for expected in (
        "app.example.com",
        "me+app.example.com@example.com",
        "app_example_com_email",
        "app_example_com_password",
    ):
        assert expected in request.summary
    # The reuse path announces itself in the prompt.
    _, summary = cap.preview("create_login", ("app.example.com",), {})
    assert summary.startswith("reuse")
    # A denial writes nothing.
    denied = Broker(
        Policy(require_approval={"vault.create_login"}),
        AuditLog(tmp_path / "audit2.jsonl"),
        Budget(),
        approver=_Approver(ApprovalOutcome.DENY),
    )
    cap2, vault2 = _capability(tmp_path / "second")
    denied.register(cap2)
    with pytest.raises(PermissionDenied):
        denied.call("vault", "create_login", "app.example.com")
    assert vault2.names() == []
