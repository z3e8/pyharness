"""Regression tests for the 2026-07 security hardening pass.

Each test pins one closed hole so it stays closed. Grouped by the vulnerability
class it guards (see agents/security-hardening-2026-07.md).
"""
from __future__ import annotations

import os
from types import ModuleType, SimpleNamespace
from urllib.parse import quote

import pytest

from pyharness import Budget, Decision, Kernel, Policy, Registry, Vault, Workspace
from pyharness.audit import AuditLog
from pyharness.broker import Broker, PermissionDenied
from pyharness.broker.remote import RemoteKernel
from pyharness.broker.capabilities import (
    FilesCapability,
    HttpSessionCapability,
    InboxCapability,
    WebCapability,
)
from pyharness.broker.capabilities.llm import LLMCapability
from pyharness.broker.capabilities.inbox import _quote
from pyharness.broker.capabilities.notify import NotifyCapability
from pyharness.broker.capabilities.skills import SkillsCapability
from pyharness.budget import BudgetExceeded
from pyharness.core.session import Session, _request_carries_secret
from pyharness.security.egress import EgressBlocked, check_url
from pyharness.security.sink import SecretSink


def _mock_transport_client(monkeypatch, handler):
    """Route the capability's own `httpx.Client(...)` construction through a
    `MockTransport` running `handler` — real httpx redirect semantics, no network.
    Returns the list of URLs actually requested, in order, so a test can assert a
    blocked hop was never contacted."""
    import httpx

    real_client = httpx.Client
    seen: list[str] = []

    def routed(request):
        seen.append(str(request.url))
        return handler(request)

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(routed)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return seen


def _broker(tmp_path):
    return Broker(Policy(), AuditLog(tmp_path / "audit.jsonl"), Budget())


# --- MCP stdio transport must not leak the parent's secrets ------------------

def test_stdio_transport_starts_from_minimal_env(monkeypatch):
    from pyharness.tools.mcp import transport as T

    captured: dict = {}

    class _FakeProc:
        def __init__(self):
            self.stdin, self.stdout, self.stderr = None, iter(()), iter(())

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "vault-pw")
    monkeypatch.setenv("PYHARNESS_SECRET_GITHUB", "gh-token")
    monkeypatch.setenv("UNRELATED_TOKEN", "t")  # unknown var: default-denied too
    monkeypatch.setattr(T.subprocess, "Popen", fake_popen)

    t = T.StdioTransport("dummy", (), env={"SERVER_OWN": "v"})
    try:
        env = captured["env"]
        assert "ANTHROPIC_API_KEY" not in env
        assert "PYHARNESS_VAULT_PASSPHRASE" not in env
        assert "PYHARNESS_SECRET_GITHUB" not in env
        assert "UNRELATED_TOKEN" not in env  # allowlist, not denylist
        assert "PATH" in env  # the basics a server needs survive
        assert env["SERVER_OWN"] == "v"  # the server's own configured env survives
    finally:
        t.close()


# --- Unknown tier must fail closed (no arbitrary model / $0 accounting) ------

def test_complete_rejects_unknown_tier(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    from pyharness.llm.client import AnthropicLLM

    llm = AnthropicLLM()
    # A raw model id passed as a tier is the exploit vector — rejected before any
    # network call, so it can neither run an unpriced model nor bill $0.
    with pytest.raises(ValueError):
        llm.complete(messages=[{"role": "user", "content": "hi"}], tier="claude-3-5-sonnet")
    with pytest.raises(ValueError):
        llm.complete(messages=[{"role": "user", "content": "hi"}], tier="claude-opus-4-8")


# --- Bare-name op dispatch must be unambiguous -------------------------------

def test_call_op_resolves_core_over_noncore(tmp_path):
    b = _broker(tmp_path)
    ws = Workspace(tmp_path)
    b.register(FilesCapability(ws))  # core: exports read/write
    b.register(InboxCapability(ws), core=False)  # non-core: also exports read
    b.call_op("write", "a.txt", "hi")
    # `read` resolves to the core files.read, never the non-core inbox.read.
    assert b.call_op("read", "a.txt") == "hi"


def test_call_op_rejects_unknown_and_ambiguous(tmp_path):
    class _Dummy:
        def __init__(self, name):
            self.name = name

        def exports(self):
            return {"dup": lambda: self.name}

    b = _broker(tmp_path)
    b.register(FilesCapability(Workspace(tmp_path)))
    with pytest.raises(KeyError):
        b.call_op("nonexistent_op")
    b.register(_Dummy("a"))
    b.register(_Dummy("b"))
    with pytest.raises(KeyError):
        b.call_op("dup")  # two core capabilities own it → refuse, don't guess


# --- A secret attached to a request always needs approval --------------------

def test_request_carries_secret_predicate():
    assert _request_carries_secret("http.request", (None, "GET", "http://x"), {"auth": "k"})
    assert _request_carries_secret("http.request", (None, "GET", "http://x"), {"secret_fields": {"p": "k"}})
    assert not _request_carries_secret("http.request", (None, "GET", "http://x"), {})
    assert _request_carries_secret("web.fetch", ("http://x", "k"), {})  # auth positional
    assert _request_carries_secret("web.fetch", ("http://x",), {"auth": "k"})
    assert not _request_carries_secret("web.fetch", ("http://x",), {})


def test_http_preview_names_secret_without_value(tmp_path):
    http = HttpSessionCapability(Workspace(tmp_path), vault=Vault({"k": "S3CRET"}))
    _, summary = http.preview("request", (None, "GET", "http://x"), {"auth": "k", "auth_style": "header"})
    assert "auth=k" in summary and "S3CRET" not in summary


def test_secret_carrying_read_is_grantable_per_host(tmp_path):
    http = HttpSessionCapability(Workspace(tmp_path))
    scope = http.scope("request", (None, "GET", "https://api.example.com/x"), {"auth": "k"})
    assert scope is not None and scope.action_class == "http" and scope.target == "api.example.com"
    # a plain unauthenticated GET is free, so it needs no grant scope
    assert http.scope("request", (None, "GET", "https://api.example.com/x"), {}) is None

    web = WebCapability(http=http)
    s = web.scope("fetch", ("https://api.example.com/x", "k"), {})
    assert s is not None and s.action_class == "http" and s.target == "api.example.com"
    assert web.scope("fetch", ("https://api.example.com/x",), {}) is None


def test_secret_carrying_request_never_follows_redirects(tmp_path, monkeypatch):
    import httpx

    def handler(request):
        if request.url.host == "198.51.100.10":
            return httpx.Response(302, headers={"location": "http://198.51.100.11/elsewhere"})
        return httpx.Response(200, text="other origin")

    seen = _mock_transport_client(monkeypatch, handler)
    http = HttpSessionCapability(Workspace(tmp_path), vault=Vault({"k": "S3CRET"}))
    result = http.request(
        None, "GET", "http://198.51.100.10/start", auth="k", auth_style="header", auth_name="X-Key"
    )
    assert result["status"] == 302  # the 3xx returns as-is; the agent re-decides auth
    assert seen == ["http://198.51.100.10/start"]  # credential never resent to the Location
    result = http.request(None, "GET", "http://198.51.100.10/start")  # no secret → followed
    assert result["status"] == 200 and result["text"] == "other origin"


# --- Screenshot leaks a secret's pixels the same way look does ---------------

def test_screenshot_gated_after_injected_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    session = Session(tmp_path, unsafe_in_process=True)
    try:
        class _StubBrowser:
            injected = True

            def has_injected_secrets(self, sid):
                return self.injected

            def close_all(self):
                pass

        stub = _StubBrowser()
        session.browser = stub
        assert session.policy.decide("browser.screenshot", ("sid",), {}) is Decision.APPROVE
        assert session.policy.decide("browser.look", ("sid",), {}) is Decision.APPROVE
        stub.injected = False  # no secret typed → both are free reads
        assert session.policy.decide("browser.screenshot", ("sid",), {}) is Decision.ALLOW
    finally:
        session.close()


# --- shell.bash is approval-gated by default (readiness C1) ------------------

def test_shell_bash_requires_approval_by_default(tmp_path, monkeypatch):
    # bash runs an arbitrary program parent-side; the default policy gates it
    # on a human. With no approver wired, an unapproved call is refused and
    # nothing executes.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    session = Session(tmp_path, unsafe_in_process=True)
    try:
        assert session.policy.decide("shell.bash", ("ls",), {}) is Decision.APPROVE
        with pytest.raises(PermissionDenied):
            session.broker.namespace()["bash"]("echo pwned > marker.txt")
        assert not (session.workspace.dir / "marker.txt").exists()
    finally:
        session.close()


# --- the default kernel is the sandboxed child (readiness H3) ----------------

def test_default_session_kernel_is_out_of_process(tmp_path, monkeypatch):
    # A bare Session() must never exec() agent code in the host process — that
    # namespace can reach os.environ (API keys, vault passphrase) and the live
    # vault/broker by introspection. The child process is lazy, so nothing is
    # spawned until the first cell runs.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    session = Session(tmp_path)
    try:
        assert isinstance(session.kernel, RemoteKernel)
    finally:
        session.close()


def test_in_process_kernel_requires_explicit_unsafe_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    session = Session(tmp_path, unsafe_in_process=True)
    try:
        assert isinstance(session.kernel, Kernel)
    finally:
        session.close()


# --- Secret masking must catch URL-encoded forms -----------------------------

def test_sink_masks_percent_encoded_secret():
    sink = SecretSink(Vault({"k": "p@ss/w0rd+x"}))
    sink.resolve("k")
    encoded = quote("p@ss/w0rd+x", safe="")
    assert "***" in sink.redact(f"https://x/?key={encoded}")
    assert "p%40ss" not in sink.redact(f"https://x/?key={encoded}")
    assert "p@ss" not in sink.redact("raw p@ss/w0rd+x here")


# --- SSRF egress guard -------------------------------------------------------

def test_egress_blocks_metadata_and_nonhttp_scheme():
    with pytest.raises(EgressBlocked):
        check_url("http://169.254.169.254/latest/meta-data/")  # cloud metadata (link-local)
    with pytest.raises(EgressBlocked):
        check_url("file:///etc/passwd")
    with pytest.raises(EgressBlocked):
        check_url("http://[fe80::1]/")  # IPv6 link-local
    assert check_url("https://8.8.8.8/") == "https://8.8.8.8/"  # public IP is fine


def test_egress_private_ranges_gated_by_strict_flag(monkeypatch):
    monkeypatch.delenv("PYHARNESS_BLOCK_PRIVATE_NETWORK", raising=False)
    assert check_url("http://127.0.0.1:8080/")  # loopback allowed by default (local dev)
    assert check_url("http://10.0.0.5/")
    monkeypatch.setenv("PYHARNESS_BLOCK_PRIVATE_NETWORK", "true")
    with pytest.raises(EgressBlocked):
        check_url("http://127.0.0.1:8080/")
    with pytest.raises(EgressBlocked):
        check_url("http://10.0.0.5/")


def test_egress_dns_failure_fails_closed(monkeypatch):
    # A name that will not resolve must be refused, not waved through: fail-open
    # (M7) let an unresolvable/intermittent name past the guard unchecked.
    import socket

    from pyharness.security import egress

    def _boom(host):
        raise socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(egress, "_resolve_host", _boom)
    with pytest.raises(EgressBlocked):
        check_url("https://does-not-resolve.invalid/")
    # An IP literal never resolves — it stays allowed even with DNS "down".
    assert check_url("https://8.8.8.8/") == "https://8.8.8.8/"


def test_remote_mcp_url_is_egress_checked_at_mount(monkeypatch):
    # A `.mcp.json` (or add_mcp_server) entry pointing at an internal endpoint
    # must be refused before any request goes out — else the parent forwards
    # `secret:` creds in headers to a chosen-internal target.
    from pyharness.tools.mcp.transport import HttpTransport

    with pytest.raises(EgressBlocked):
        HttpTransport("http://169.254.169.254/mcp")  # cloud metadata (link-local)

    registry = Registry()
    with pytest.raises(EgressBlocked):
        registry.add_mcp_server("evil", url="http://169.254.169.254/mcp")


def test_packages_install_runs_with_a_scrubbed_env(monkeypatch, tmp_path):
    # A malicious package's setup.py runs at install time; pip must not inherit
    # the parent env or it could read ANTHROPIC_API_KEY / PYHARNESS_SECRET_*.
    import subprocess

    from pyharness.broker.capabilities.packages import PackagesCapability

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("PYHARNESS_SECRET_TOKEN", "tok-should-not-leak")

    venv = SimpleNamespace(
        dir=tmp_path,
        site_packages=lambda: tmp_path / "site",
    )
    seen = {}

    def _fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return SimpleNamespace(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    PackagesCapability(venv).install("requests")

    env = seen["env"]
    assert env is not None, "pip must run with an explicit scrubbed env"
    assert "ANTHROPIC_API_KEY" not in env
    assert "PYHARNESS_SECRET_TOKEN" not in env
    assert "PATH" in env  # ...but pip still gets what it legitimately needs


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_index_dir_and_db_are_owner_only(tmp_path):
    # ~/.pyharness holds the vault, profiles, and the session index — none of it
    # world-readable. The dir must be 0700 and the index db 0600 (both default to
    # world-readable under the common umask otherwise).
    from pyharness.obs.index import open_db

    db = tmp_path / "home" / ".pyharness" / "index.db"
    conn = open_db(db)
    conn.close()
    assert (db.parent.stat().st_mode & 0o777) == 0o700
    assert (db.stat().st_mode & 0o777) == 0o600


# --- SSRF via redirect: every hop is re-vetted, not just the initial url ------

def test_http_redirect_to_internal_address_is_blocked_on_the_hop(tmp_path, monkeypatch):
    import httpx

    def handler(request):
        if request.url.host == "198.51.100.10":
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        return httpx.Response(200, text="IAM-CREDENTIALS")

    seen = _mock_transport_client(monkeypatch, handler)
    http = HttpSessionCapability(Workspace(tmp_path))
    with pytest.raises(EgressBlocked):
        http.request(None, "GET", "http://198.51.100.10/start")
    # The metadata endpoint was never contacted — the hop died at the check, so
    # the internal body has no path back to the agent.
    assert seen == ["http://198.51.100.10/start"]


def test_http_redirects_to_permitted_hosts_still_follow(tmp_path, monkeypatch):
    import httpx

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://198.51.100.11/final"})
        return httpx.Response(200, text="landed")

    seen = _mock_transport_client(monkeypatch, handler)
    http = HttpSessionCapability(Workspace(tmp_path))
    result = http.request(None, "GET", "http://198.51.100.10/start")
    assert result["status"] == 200 and result["text"] == "landed"
    assert result["url"] == "http://198.51.100.11/final"
    assert seen == ["http://198.51.100.10/start", "http://198.51.100.11/final"]


def test_http_redirect_loop_is_capped(tmp_path, monkeypatch):
    import httpx

    def handler(request):
        n = int(request.url.path.lstrip("/"))
        return httpx.Response(302, headers={"location": f"http://198.51.100.10/{n + 1}"})

    seen = _mock_transport_client(monkeypatch, handler)
    http = HttpSessionCapability(Workspace(tmp_path))
    with pytest.raises(httpx.TooManyRedirects):
        http.request(None, "GET", "http://198.51.100.10/0")
    assert len(seen) <= 21  # initial request + at most _MAX_REDIRECTS hops


def test_browser_route_handler_revets_every_request():
    # The browser's per-request enforcement point (installed via context.route on
    # every session): an HTTP/JS/meta-refresh redirect or subresource fetch to a
    # blocked address is aborted; permitted and in-page (data:) urls continue.
    from pyharness.broker.capabilities.browser import _egress_route_handler

    class _Route:
        def __init__(self, url):
            self.request = SimpleNamespace(url=url)
            self.aborted = self.continued = False

        def abort(self, code=None):
            self.aborted = True

        def continue_(self):
            self.continued = True

    blocked = _Route("http://169.254.169.254/latest/meta-data/")
    _egress_route_handler(blocked)
    assert blocked.aborted and not blocked.continued

    ok = _Route("https://8.8.8.8/page")
    _egress_route_handler(ok)
    assert ok.continued and not ok.aborted

    inline = _Route("data:text/plain,hi")
    _egress_route_handler(inline)
    assert inline.continued and not inline.aborted  # never leaves the page


# --- IMAP command injection --------------------------------------------------

def test_inbox_quote_rejects_control_chars():
    with pytest.raises(ValueError):
        _quote("inbox\r\nA1 UID STORE 1 +FLAGS (\\Deleted)")
    assert _quote('a folder "x"') == '"a folder \\"x\\""'  # normal quoting still works


def test_inbox_read_rejects_nonnumeric_id(tmp_path):
    inbox = InboxCapability(Workspace(tmp_path))
    with pytest.raises(ValueError):
        inbox.read("1 (BODY[]) \r\nA1 NOOP")  # rejected before any connection


# --- notify cannot emit terminal control sequences ---------------------------

def test_notify_strips_control_and_ansi():
    seen: dict = {}
    cap = NotifyCapability(on_event=lambda kind, text, **e: seen.update(text=text), desktop=None)
    cap.notify("hello\x1b[2J\x07 world")
    assert "\x1b" not in seen["text"] and "\x07" not in seen["text"]
    assert "hello" in seen["text"] and "world" in seen["text"]


# --- A skill cannot shadow a core capability name ----------------------------

def test_save_skill_refuses_core_capability_name(tmp_path):
    reg = Registry()
    reg.register(ModuleType("http"), source="core", name="http")
    cap = SkillsCapability(reg, tmp_path / "skills")
    with pytest.raises(ValueError):
        cap.save_skill("http", "desc", "instructions")
    cap.save_skill("mytool", "desc", "do the thing")  # a fresh learned name is fine
    assert reg.info("mytool") is not None


def test_skill_name_rejects_traversal(tmp_path):
    cap = SkillsCapability(Registry(), tmp_path / "skills")
    with pytest.raises(ValueError):
        cap.save_skill("../evil", "desc", "x")
    with pytest.raises(ValueError):
        cap.record_skill_use("../evil", "worked")


# --- An LLM-worker fan-out cannot overshoot the budget ------------------------

def test_llm_workers_check_budget_before_each_completion():
    budget = Budget(limit_usd=1.0)
    budget.spent_usd = 2.0  # already over
    cap = LLMCapability(llm=object(), budget=budget)
    with pytest.raises(BudgetExceeded):
        cap.run("task")  # propagates, like the orchestrator's own overrun
    results = cap.map_llm(["a", "b"])  # fan-out turns it into per-task error data
    assert results and all(not r.ok for r in results)
