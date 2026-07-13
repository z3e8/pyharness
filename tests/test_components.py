from types import SimpleNamespace

import pytest

from pyharness import ActionCategory, Budget, Decision, Policy, Registry, Vault, Workspace
from pyharness.audit import AuditLog
from pyharness.broker import Broker, PermissionDenied
from pyharness.broker.capabilities import (
    FilesCapability,
    HttpSessionCapability,
    WebCapability,
)
from pyharness.budget import BudgetExceeded
from pyharness.core.kernel import Kernel


def _broker(tmp_path, policy=None, approver=None):
    audit = AuditLog(tmp_path / "audit.jsonl")
    return Broker(policy or Policy(), audit, Budget(), approver=approver)


def test_kernel_persists_and_captures(tmp_path):
    kernel = Kernel({})
    assert kernel.run("x = 41") == "(no output)"
    assert kernel.run("print(x + 1)") == "42"
    assert "ZeroDivisionError" in kernel.run("print(1 / 0)")


def test_workspace_confines_relative_paths(tmp_path):
    ws = Workspace(tmp_path)
    files = FilesCapability(ws)
    files.write("a.txt", "hi")
    assert files.read("a.txt") == "hi"
    files.edit("a.txt", "hi", "bye")
    assert (ws.dir / "a.txt").read_text() == "bye"


def test_workspace_rejects_escape(tmp_path):
    ws = Workspace(tmp_path)
    import pytest

    with pytest.raises(ValueError):
        ws.path("../escape.txt")
    with pytest.raises(ValueError):
        ws.path("/etc/passwd")


def test_broker_routes_and_audits(tmp_path):
    broker = _broker(tmp_path)
    broker.register(FilesCapability(Workspace(tmp_path)))
    ns = broker.namespace()
    assert "read" in ns and "write" in ns
    ns["write"]("note.txt", "data")
    assert ns["read"]("note.txt") == "data"
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert any('"files.write"' in line for line in lines)


def test_policy_deny(tmp_path):
    broker = _broker(tmp_path, policy=Policy(deny={"files"}))
    broker.register(FilesCapability(Workspace(tmp_path)))
    import pytest

    with pytest.raises(PermissionDenied):
        broker.namespace()["write"]("x.txt", "y")


def test_audit_tail_returns_recent_calls_stripped(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record(action="files.write", ok=True, args="'a.txt', 'x'")
    audit.record(action="http.request", ok=True, args="'POST', 'http://x'")
    tail = audit.tail(limit=5)
    assert [e["action"] for e in tail] == ["files.write", "http.request"]
    # Internal chain fields never leak to the reflecting agent.
    assert all("hash" not in e and "prev" not in e for e in tail)
    # Prefix filter narrows to one capability; limit keeps the most recent.
    assert [e["action"] for e in audit.tail(action="http")] == ["http.request"]
    assert [e["action"] for e in audit.tail(limit=1)] == ["http.request"]


def test_history_capability_reads_own_actions(tmp_path):
    from pyharness.broker.capabilities import HistoryCapability

    broker = _broker(tmp_path)
    broker.register(FilesCapability(Workspace(tmp_path)))
    broker.register(HistoryCapability(broker.audit))
    ns = broker.namespace()
    ns["write"]("note.txt", "data")
    seen = ns["history"]()
    # The agent sees its own prior write; the current history call isn't logged
    # until it returns, so it can't see itself.
    actions = [e["action"] for e in seen]
    assert "files.write" in actions and "history.history" not in actions


def test_policy_approval(tmp_path):
    seen = {}

    def approver(request):
        seen["action"] = request.action
        seen["category"] = request.category
        return True

    broker = _broker(tmp_path, policy=Policy(require_approval={"files.write"}), approver=approver)
    broker.register(FilesCapability(Workspace(tmp_path)))
    broker.namespace()["write"]("x.txt", "y")
    assert seen["action"] == "files.write"
    # Files has no preview hook, so the broker falls back to a conservative class.
    assert seen["category"] is ActionCategory.OUTWARD


def test_budget_records_and_limits():
    b = Budget(limit_usd=0.01)
    b.check()  # under limit, fine
    b.record("claude-opus-4-8", 0.02)
    import pytest

    with pytest.raises(BudgetExceeded):
        b.check()


def _example_tool():
    """A throwaway registry tool: a module named `widget` exposing `double`."""
    from types import ModuleType

    module = ModuleType("widget")
    module.__doc__ = "Widget helpers."

    def double(n: int) -> int:
        """Return twice the input."""
        return n * 2

    double.__module__ = "widget"  # so _public_functions discovers it
    module.double = double
    return module


def test_registry_discovers_describes_and_uses_a_tool():
    reg = Registry()
    reg.register(_example_tool(), source="installed", keywords=("widget",))
    assert "# widget" in reg.search("widget")  # found by keyword
    assert "double" in reg.describe("widget")  # signatures come from describe
    assert reg.use("widget").double(21) == 42  # loaded and callable


def test_non_core_capability_is_gated_but_not_a_builtin(tmp_path):
    broker = _broker(tmp_path)
    broker.register(FilesCapability(Workspace(tmp_path)), core=False)
    # Not surfaced as a bare-name builtin, in-process or (via op_names) in the child.
    assert "read" not in broker.namespace()
    assert "read" not in broker.op_names()
    # But still registered and callable through the broker, with auditing.
    import json

    broker.call("files", "write", "note.txt", "data")
    actions = [json.loads(line).get("action") for line in
               (tmp_path / "audit.jsonl").read_text().strip().splitlines()]
    assert "files.write" in actions


def test_as_tool_module_surfaces_a_capability_through_the_registry(tmp_path):
    approved = []

    def approver(request):
        approved.append(request.action)
        return True

    broker = _broker(tmp_path, policy=Policy(require_approval={"files.write"}),
                     approver=approver)
    broker.register(FilesCapability(Workspace(tmp_path)), core=False)
    reg = Registry()
    reg.register(broker.as_tool_module("files", summary="Workspace files."),
                 source="core", name="files")

    # describe_tool shows the real signatures/docstrings, not the (*args, **kwargs) proxy.
    details = reg.describe("files")
    assert "read(path: 'str')" in details and "write(path: 'str', content: 'str')" in details
    # Loading and calling routes through the broker: the write is gated + approved.
    module = reg.use("files")
    module.write("note.txt", "data")
    assert approved == ["files.write"]
    assert (Workspace(tmp_path).dir / "note.txt").read_text() == "data"


def test_subagent_session_cap():
    from pyharness.broker.capabilities import AgentsCapability, SubAgentLimitExceeded

    class StubLLM:
        def complete(self, *, system, messages, tier="cheap", tools=None, max_tokens=None):
            from pyharness.llm.client import Completion

            return Completion(text="ok", tool_calls=[], content=[])

    import pytest

    agents = AgentsCapability(StubLLM(), session_cap=2)
    assert agents.agent("one") == "ok"
    assert agents.agent("two") == "ok"
    with pytest.raises(SubAgentLimitExceeded):
        agents.agent("three")

    # In fan-out the cap surfaces as failed Results, not an exception.
    results = AgentsCapability(StubLLM(), session_cap=1).map_agents(["a", "b", "c"])
    assert sum(r.ok for r in results) == 1
    assert any("cap reached" in (r.error or "") for r in results)


def test_vault_never_via_namespace(tmp_path):
    # The vault is reachable by trusted code but is not a kernel function.
    broker = _broker(tmp_path)
    broker.register(FilesCapability(Workspace(tmp_path)))
    assert "get" not in broker.namespace()
    assert Vault({"token": "secret"}).get("token") == "secret"


def test_encrypted_file_roundtrip_and_wrong_passphrase(tmp_path):
    from pyharness.security.vault import EncryptedFile

    path = tmp_path / "secrets.enc"
    EncryptedFile(path, "correct horse").save({"github": "ghp_123", "openai": "sk-xyz"})
    assert EncryptedFile(path, "correct horse").load() == {"github": "ghp_123", "openai": "sk-xyz"}
    assert EncryptedFile(path, "correct horse").names() == ["github", "openai"]
    assert (path.stat().st_mode & 0o777) == 0o600

    with pytest.raises(Exception):  # authenticated decryption fails on wrong passphrase
        EncryptedFile(path, "wrong").load()


def test_vault_resolution_order_and_names(tmp_path, monkeypatch):
    from pyharness.security.vault import EncryptedFile

    path = tmp_path / "secrets.enc"
    EncryptedFile(path, "pw").save({"only_in_file": "F", "shared": "from_file"})
    monkeypatch.setenv("PYHARNESS_SECRET_ONLY_IN_ENV", "E")
    vault = Vault({"shared": "from_dict"}, file=EncryptedFile(path, "pw"))

    assert vault.get("shared") == "from_dict"  # dict wins
    assert vault.get("only_in_env") == "E"  # env next
    assert vault.get("only_in_file") == "F"  # file last
    assert vault.names() == ["only_in_env", "only_in_file", "shared"]


def test_secrets_capability_exposes_names_not_values(tmp_path):
    from pyharness.broker.capabilities import SecretsCapability

    broker = _broker(tmp_path)
    broker.register(SecretsCapability(Vault({"github": "ghp_secret"})))
    ns = broker.namespace()
    assert ns["secrets"]() == ["github"]
    assert "get" not in ns  # the value-returning method is never exposed


def test_secret_sink_resolves_records_and_redacts():
    from pyharness.security.sink import SecretSink

    sink = SecretSink(Vault({"pw": "hunter2"}))
    assert sink.resolve("pw") == "hunter2"  # cleartext for the injection point
    # Every string the agent reads back is masked, one level deep into mappings.
    assert sink.redact("you typed hunter2") == "you typed ***"
    assert sink.redacted({"url": "http://x?t=hunter2", "n": 1, "h": {"echo": "hunter2"}}) == {
        "url": "http://x?t=***",
        "n": 1,
        "h": {"echo": "***"},
    }


def test_secret_sink_without_vault_raises():
    from pyharness.security.sink import SecretSink

    sink = SecretSink(None)
    with pytest.raises(RuntimeError):
        sink.resolve("pw")
    assert sink.redact("nothing injected") == "nothing injected"


class _FakeResp:
    def __init__(self, status=200, text="ok", url="http://x", content_type="text/plain"):
        import datetime

        self.status_code = status
        self.text = text
        self.url = url
        self.headers = {"content-type": content_type}
        self.elapsed = datetime.timedelta(milliseconds=2)


class _FakeClient:
    """Records every request; shared instances let tests assert cookie-jar reuse
    (one client per session) versus one-shot use (a fresh client per call)."""

    instances: list = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list = []
        self.closed = False
        _FakeClient.instances.append(self)

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResp(url=url)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_httpx(monkeypatch):
    import httpx

    _FakeClient.instances = []
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return _FakeClient


def test_web_fetch_injects_auth_parent_side(tmp_path, fake_httpx):
    http = HttpSessionCapability(Workspace(tmp_path), vault=Vault({"k": "S3CRET"}))
    web = WebCapability(llm=None, http=http)

    web.web_fetch("http://x", auth="k")
    assert fake_httpx.instances[-1].calls[-1]["headers"]["Authorization"] == "Bearer S3CRET"

    web.web_fetch("http://x", auth="k", auth_style="header", auth_name="X-API-Key")
    assert fake_httpx.instances[-1].calls[-1]["headers"]["X-API-Key"] == "S3CRET"

    web.web_fetch("http://x", auth="k", auth_style="query", auth_name="api_key")
    assert fake_httpx.instances[-1].calls[-1]["params"] == {"api_key": "S3CRET"}

    web.web_fetch("http://x", auth="k", auth_style="basic", user="alice")
    import base64

    expected = "Basic " + base64.b64encode(b"alice:S3CRET").decode()
    assert fake_httpx.instances[-1].calls[-1]["headers"]["Authorization"] == expected


def test_html_to_text_strips_markup_and_noise():
    from pyharness.broker.capabilities.http import html_to_text

    html = (
        "<html><head><title>T</title><style>.a{color:red}</style>"
        "<script>var x=1;</script></head><body>"
        "<h1>Best Jackets</h1><p>Baracuta   G9 is\n  great.</p>"
        "<script>track()</script><div>J.Crew Harrington</div></body></html>"
    )
    text = html_to_text(html)
    assert "Best Jackets" in text
    assert "Baracuta G9 is great." in text  # runs of whitespace collapsed
    assert "J.Crew Harrington" in text
    assert "color:red" not in text  # <style> dropped
    assert "var x" not in text and "track()" not in text  # <script> dropped
    assert "<" not in text  # no tags survive


def test_web_search_declares_direct_caller():
    # The cheap tier (haiku) rejects the web_search tool unless it is called
    # directly; assert the declaration carries allowed_callers=["direct"] so the
    # server runs the search rather than requiring programmatic tool calling.
    from types import SimpleNamespace

    from pyharness.llm.client import AnthropicLLM

    llm = AnthropicLLM()
    captured: dict = {}

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text="jackets!")],
            )

    class _Messages:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return _Stream()

    class _Client:
        messages = _Messages()

        def with_options(self, **kwargs):
            captured["options"] = kwargs
            return self

    llm._client = _Client()
    assert llm.web_search("harrington jackets", tier="cheap") == "jackets!"
    assert captured["model"] == "claude-haiku-4-5"
    assert captured["tools"] == [
        {"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]}
    ]
    # Search calls get a read timeout well past the default completion budget.
    assert captured["options"]["timeout"].read == 600.0


def test_web_fetch_extracts_html_but_passes_other_types_through(tmp_path, monkeypatch):
    import httpx

    def client_returning(content_type, body):
        class _Client:
            def __init__(self, **kwargs):
                pass

            def request(self, method, url, **kwargs):
                return _FakeResp(text=body, url=url, content_type=content_type)

            def close(self):
                pass

        return _Client

    web = WebCapability(llm=None, http=HttpSessionCapability(Workspace(tmp_path)))

    monkeypatch.setattr(
        httpx, "Client",
        client_returning("text/html; charset=utf-8", "<html><body><script>x</script><p>Hello <b>world</b></p></body></html>"),
    )
    assert web.web_fetch("http://x").strip() == "Hello world"

    # A non-HTML body is returned verbatim, not run through the reducer.
    monkeypatch.setattr(httpx, "Client", client_returning("application/json", '{"a": 1}'))
    assert web.web_fetch("http://x") == '{"a": 1}'


def test_http_session_reuses_one_client(tmp_path, fake_httpx):
    http = HttpSessionCapability(Workspace(tmp_path))
    sid = http.open_session()
    http.request(sid, "GET", "http://a")
    http.request(sid, "GET", "http://b")
    # One client for the session (cookie jar shared across both requests).
    assert len(fake_httpx.instances) == 1
    assert [c["url"] for c in fake_httpx.instances[0].calls] == ["http://a", "http://b"]
    http.close_session(sid)
    assert fake_httpx.instances[0].closed


def test_http_request_returns_structured_result(tmp_path, fake_httpx):
    http = HttpSessionCapability(Workspace(tmp_path))
    r = http.request(None, "GET", "http://x")
    assert r["status"] == 200
    assert r["text"] == "ok"
    assert r["url"] == "http://x"
    assert r["truncated"] is False
    assert r["elapsed_ms"] == 2


def test_http_one_shot_uses_fresh_client_each_call(tmp_path, fake_httpx):
    http = HttpSessionCapability(Workspace(tmp_path))
    http.request(None, "GET", "http://a")
    http.request(None, "GET", "http://b")
    assert len(fake_httpx.instances) == 2
    assert all(c.closed for c in fake_httpx.instances)  # transient clients close


def test_http_injects_secret_into_body(tmp_path, fake_httpx):
    http = HttpSessionCapability(Workspace(tmp_path), vault=Vault({"pw": "hunter2"}))
    http.request(None, "POST", "http://x", json={"user": "me"}, secret_fields={"password": "pw"})
    body = fake_httpx.instances[-1].calls[-1]["json"]
    assert body == {"user": "me", "password": "hunter2"}


def test_http_masks_injected_secret_echoed_in_response(tmp_path, monkeypatch):
    # A query-string auth secret survives into the final url, and a body secret
    # can be echoed in the response text/headers. None may round-trip to the
    # agent — the request's sink masks every string field before returning.
    import datetime

    import httpx

    class _EchoResp:
        def __init__(self, url, token):
            self.status_code = 200
            self.url = f"{url}?api_key={token}"  # query secret survives into final url
            self.text = "server echoed hunter2"  # body secret reflected in response
            self.headers = {"x-echo": "hunter2"}  # ...and in a header
            self.elapsed = datetime.timedelta(milliseconds=2)

    class _EchoClient:
        def __init__(self, **kwargs):
            pass

        def request(self, method, url, **kwargs):
            return _EchoResp(url, (kwargs.get("params") or {}).get("api_key", ""))

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _EchoClient)
    http = HttpSessionCapability(Workspace(tmp_path), vault=Vault({"k": "hunter2", "pw": "hunter2"}))
    r = http.request(
        None, "POST", "http://x", auth="k", auth_style="query", auth_name="api_key",
        json={"user": "me"}, secret_fields={"password": "pw"},
    )
    assert "hunter2" not in r["url"] and "***" in r["url"]
    assert "hunter2" not in r["text"] and "***" in r["text"]
    assert "hunter2" not in str(r["headers"]) and r["headers"]["x-echo"] == "***"


def test_http_upload_reads_file_parent_side(tmp_path, fake_httpx):
    ws = Workspace(tmp_path)
    (ws.dir / "resume.txt").write_text("CV")
    http = HttpSessionCapability(ws)
    http.request(None, "POST", "http://x", files=[["file", "resume.txt"]])
    assert fake_httpx.instances[-1].calls[-1]["files"] == [("file", ("resume.txt", b"CV"))]


def test_http_upload_rejects_workspace_escape(tmp_path, fake_httpx):
    http = HttpSessionCapability(Workspace(tmp_path))
    with pytest.raises(ValueError):
        http.request(None, "POST", "http://x", files=[["file", "../secret.txt"]])


def test_policy_approve_if_predicate():
    pol = Policy(approve_if=[lambda a, ar, kw: a == "http.request" and ar[1] == "POST"])
    assert pol.decide("http.request", ("sid", "POST", "http://x"), {}) is Decision.APPROVE
    assert pol.decide("http.request", ("sid", "GET", "http://x"), {}) is Decision.ALLOW


def test_mutating_http_requires_approval(tmp_path, fake_httpx):
    from pyharness.core.session import _is_mutating_http

    prompted = []

    def approver(request):
        prompted.append(request.args[1])
        return False

    broker = _broker(tmp_path, policy=Policy(approve_if=[_is_mutating_http]), approver=approver)
    broker.register(HttpSessionCapability(Workspace(tmp_path)))
    ns = broker.namespace()

    ns["request"](None, "GET", "http://x")  # read: no approval prompt
    assert prompted == []

    with pytest.raises(PermissionDenied):
        ns["request"](None, "POST", "http://x")  # write: gated, and denied here
    assert prompted == ["POST"]


# --- Browser lane (C2) -------------------------------------------------------
# Playwright is an optional extra, so these drive the capability against a fake
# page — no chromium binary, runs in CI. A live smoke test would be
# @pytest.mark.skipif(playwright missing) and is intentionally left out of CI.


class _FakePage:
    """Records every action; `_text` is what inner_text returns (used to prove
    injected secrets get masked on read-back)."""

    def __init__(self, url="http://start", text="page body"):
        self.url = url
        self._text = text
        self.calls: list = []

    def goto(self, url, wait_until=None):
        self.url = url
        self.calls.append(("goto", url, wait_until))
        return SimpleNamespace(status=200)  # playwright Response.status

    def title(self):
        return "Title"

    def click(self, selector):
        self.calls.append(("click", selector))

    def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    def inner_text(self, selector):
        return self._text

    def screenshot(self, path):
        self.calls.append(("screenshot", path))


def _browser_with_fake(ws, vault=None, text="page body"):
    """A BrowserCapability with one fake session injected under id "sid" — skips
    open_browser so no real driver/chromium is launched."""
    from pyharness.broker.capabilities.browser import BrowserCapability, _BrowserSession
    from pyharness.security.sink import SecretSink

    cap = BrowserCapability(ws, vault=vault)
    page = _FakePage(text=text)
    cap._sessions["sid"] = _BrowserSession(
        browser=_FakeClient(), context=_FakeClient(), page=page, sink=SecretSink(vault)
    )
    return cap, page


def test_browser_actions_return_structured_result(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path))
    r = cap.goto("sid", "http://x")
    assert r == {"url": "http://x", "title": "Title", "status": 200}
    assert cap.click("sid", "#go")["title"] == "Title"
    assert cap.fill("sid", "#name", "Ada")["url"] == "http://x"
    assert ("fill", "#name", "Ada") in page.calls


def test_browser_fill_secret_injects_parent_side_and_hides_value(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path), vault=Vault({"pw": "hunter2"}))
    result = cap.fill_secret("sid", "#password", "pw")
    # The cleartext was typed into the page but never returned to the caller.
    assert ("fill", "#password", "hunter2") in page.calls
    assert "hunter2" not in repr(result)


def test_browser_masks_injected_secret_in_returned_url(tmp_path):
    # A secret can land in the url (e.g. a GET query string); the result's url
    # field must mask it just like read_text does — no round-trip to agent code.
    cap, page = _browser_with_fake(Workspace(tmp_path), vault=Vault({"pw": "hunter2"}))
    cap.fill_secret("sid", "#password", "pw")
    page.url = "http://x/callback?token=hunter2"  # secret landed in the query string
    r = cap.click("sid", "#submit")  # click doesn't change the fake url
    assert "hunter2" not in r["url"]
    assert "***" in r["url"]


def test_browser_read_text_masks_injected_secret(tmp_path):
    cap, page = _browser_with_fake(
        Workspace(tmp_path), vault=Vault({"pw": "hunter2"}), text="you typed hunter2 ok"
    )
    cap.fill_secret("sid", "#password", "pw")
    r = cap.read_text("sid")
    assert "hunter2" not in r["text"]
    assert "***" in r["text"]


def test_browser_audit_summary_never_holds_secret_value(tmp_path):
    from pyharness.util import summarize_args

    # The agent passes the secret *name*, so the audited arg rendering can never
    # contain the value — same invariant the C1 http body-injection test relies on.
    assert "hunter2" not in summarize_args(("sid", "#password", "pw"), {})


def test_browser_screenshot_rejects_workspace_escape(tmp_path):
    cap, _ = _browser_with_fake(Workspace(tmp_path))
    with pytest.raises(ValueError):
        cap.screenshot("sid", "../outside.png")


def test_browser_unknown_session_raises(tmp_path):
    cap, _ = _browser_with_fake(Workspace(tmp_path))
    with pytest.raises(KeyError):
        cap.goto("nope", "http://x")


def test_mutating_browser_requires_approval(tmp_path):
    from pyharness.broker.capabilities.browser import MUTATING_ACTIONS

    prompted = []

    def approver(request):
        prompted.append(request)
        return False

    cap, _ = _browser_with_fake(Workspace(tmp_path), vault=Vault({"pw": "hunter2"}))
    broker = _broker(tmp_path, policy=Policy(require_approval=set(MUTATING_ACTIONS)), approver=approver)
    broker.register(cap)
    ns = broker.namespace()

    ns["goto"]("sid", "http://x")  # navigation: free
    ns["read_text"]("sid")  # read: free
    assert prompted == []

    with pytest.raises(PermissionDenied):
        ns["click"]("sid", "#submit")  # state-changing: gated, denied here
    assert [r.action for r in prompted] == ["browser.click"]
    # The preview enriches the confirmation with the page the click lands on.
    assert prompted[0].category is ActionCategory.OUTWARD
    assert "#submit" in prompted[0].summary and "http://x" in prompted[0].summary


# --- Approval preview & taxonomy (C5) ----------------------------------------


def test_http_preview_classifies_and_summarizes(tmp_path):
    http = HttpSessionCapability(Workspace(tmp_path))
    # POST: outward, and the summary shows method, url, and body field *names*.
    cat, summary = http.preview("request", (None, "POST", "http://api/x"), {"json": {"a": 1, "b": 2}})
    assert cat is ActionCategory.OUTWARD
    assert "POST" in summary and "http://api/x" in summary and "a, b" in summary
    # DELETE is the one method the harness knows is irreversible.
    cat, _ = http.preview("request", (None, "DELETE", "http://api/x"), {})
    assert cat is ActionCategory.IRREVERSIBLE


def test_http_preview_shows_body_field_names_not_values(tmp_path):
    # The body can hold workspace data; the confirmation names the fields but
    # never dumps their values.
    http = HttpSessionCapability(Workspace(tmp_path))
    _, summary = http.preview("request", (None, "POST", "http://x"), {"data": {"resume": "PRIVATE"}})
    assert "resume" in summary and "PRIVATE" not in summary


def test_browser_preview_masks_secret_in_page_url(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path), vault=Vault({"pw": "hunter2"}))
    cap.fill_secret("sid", "#password", "pw")
    page.url = "http://x/cb?token=hunter2"  # secret landed in the page's query string
    cat, summary = cap.preview("click", ("sid", "#submit"), {})
    assert cat is ActionCategory.OUTWARD
    assert "hunter2" not in summary and "***" in summary


def test_skills_and_packages_preview_categories(tmp_path):
    from pyharness.broker.capabilities import PackagesCapability, SkillsCapability
    from pyharness.core.session_venv import SessionVenv

    skills = SkillsCapability(Registry(), tmp_path / "skills")
    cat, summary = skills.preview("save_skill", ("greeter",), {})
    assert cat is ActionCategory.LOCAL and "greeter" in summary

    packages = PackagesCapability(SessionVenv())
    cat, summary = packages.preview("install", ("requests",), {})
    assert cat is ActionCategory.OUTWARD and "requests" in summary


def test_broker_records_category_in_audit(tmp_path):
    import json

    broker = _broker(
        tmp_path, policy=Policy(require_approval={"packages.install"}), approver=lambda r: False
    )
    from pyharness.broker.capabilities import PackagesCapability
    from pyharness.core.session_venv import SessionVenv

    broker.register(PackagesCapability(SessionVenv()))
    with pytest.raises(PermissionDenied):
        broker.namespace()["install"]("requests")
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    approve = next(e for e in entries if e.get("decision") == "approve")
    assert approve["category"] == "outward" and approve["approved"] is False


def test_shell_subprocess_has_no_secrets(tmp_path, monkeypatch):
    # shell.bash runs commands parent-side, where vault secrets live in the
    # environment. The subprocess must get a scrubbed environment so a command
    # like `echo $SECRET` can't read a secret the agent only knows by name.
    from pyharness.broker.capabilities import ShellCapability

    monkeypatch.setenv("PYHARNESS_SECRET_TOKEN", "supersecret")
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "hunter2")
    monkeypatch.setenv("PATH_KEPT", "ok")
    shell = ShellCapability(Workspace(tmp_path))
    out = shell.bash('echo "tok=$PYHARNESS_SECRET_TOKEN pass=$PYHARNESS_VAULT_PASSPHRASE kept=$PATH_KEPT"')
    assert "supersecret" not in out
    assert "hunter2" not in out
    assert "kept=ok" in out  # non-secret env still passes through
