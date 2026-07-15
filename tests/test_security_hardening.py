"""Regression tests for the 2026-07 security hardening pass.

Each test pins one closed hole so it stays closed. Grouped by the vulnerability
class it guards (see agents/security-hardening-2026-07.md).
"""
from __future__ import annotations

from types import ModuleType
from urllib.parse import quote

import pytest

from pyharness import Budget, Decision, Policy, Registry, Vault, Workspace
from pyharness.audit import AuditLog
from pyharness.broker import Broker
from pyharness.broker.capabilities import (
    FilesCapability,
    HttpSessionCapability,
    InboxCapability,
    WebCapability,
)
from pyharness.broker.capabilities.agents import AgentsCapability
from pyharness.broker.capabilities.inbox import _quote
from pyharness.broker.capabilities.notify import NotifyCapability
from pyharness.broker.capabilities.skills import SkillsCapability
from pyharness.budget import BudgetExceeded
from pyharness.core.session import Session, _request_carries_secret
from pyharness.security.egress import EgressBlocked, check_url
from pyharness.security.sink import SecretSink


class _FakeResp:
    def __init__(self, url="http://x"):
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
        return _FakeResp(url=url)

    def close(self):
        pass


@pytest.fixture
def fake_httpx(monkeypatch):
    import httpx

    _FakeClient.instances = []
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return _FakeClient


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

    web = WebCapability(llm=None, http=http)
    s = web.scope("fetch", ("https://api.example.com/x", "k"), {})
    assert s is not None and s.action_class == "http" and s.target == "api.example.com"
    assert web.scope("fetch", ("https://api.example.com/x",), {}) is None


def test_secret_carrying_request_disables_redirects(tmp_path, fake_httpx):
    http = HttpSessionCapability(Workspace(tmp_path), vault=Vault({"k": "S3CRET"}))
    http.request(None, "GET", "http://nonexistent.invalid", auth="k")
    assert fake_httpx.instances[-1].calls[-1]["follow_redirects"] is False
    http.request(None, "GET", "http://nonexistent.invalid")
    assert fake_httpx.instances[-1].calls[-1]["follow_redirects"] is True


# --- Screenshot leaks a secret's pixels the same way look does ---------------

def test_screenshot_gated_after_injected_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    session = Session(tmp_path)
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


# --- A sub-agent fan-out cannot overshoot the budget -------------------------

def test_agents_check_budget_before_each_completion():
    budget = Budget(limit_usd=1.0)
    budget.spent_usd = 2.0  # already over
    cap = AgentsCapability(llm=object(), budget=budget)
    with pytest.raises(BudgetExceeded):
        cap.agent("task")  # propagates, like the orchestrator's own overrun
    results = cap.map_agents(["a", "b"])  # fan-out turns it into per-task error data
    assert results and all(not r.ok for r in results)
