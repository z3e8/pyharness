import base64
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


def test_truncate_keeps_head_and_tail(tmp_path):
    from pyharness.util import truncate

    text = "H" * 500 + "M" * 40_000 + "T" * 500
    out = truncate(text)
    assert out.startswith("H") and out.rstrip().endswith("T")  # both ends survive
    assert "truncated" in out and len(out) < len(text)


def test_workspace_confines_relative_paths(tmp_path):
    ws = Workspace(tmp_path)
    files = FilesCapability(ws)
    files.write("a.txt", "hi")
    assert files.read("a.txt") == "hi"
    files.edit("a.txt", "hi", "bye")
    assert (ws.dir / "a.txt").read_text() == "bye"


def test_read_pages_by_line_and_never_truncates(tmp_path):
    ws = Workspace(tmp_path)
    files = FilesCapability(ws)
    content = "".join(f"line {i}\n" for i in range(2000))
    files.write("log.txt", content)
    # A window pages the file; the full read returns every byte (no 10k cap).
    assert files.read("log.txt", offset=2, limit=3) == "line 2\nline 3\nline 4\n"
    full = files.read("log.txt")
    assert full == content and len(full) > 10_000


def test_bash_output_not_truncated(tmp_path):
    from pyharness.broker.capabilities.shell import ShellCapability

    out = ShellCapability(Workspace(tmp_path)).bash("printf 'z%.0s' $(seq 1 20000)")
    assert len(out) == 20_000 and "truncated" not in out


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
    assert "read(path: 'str'" in details and "write(path: 'str', content: 'str')" in details
    # Loading and calling routes through the broker: the write is gated + approved.
    module = reg.use("files")
    module.write("note.txt", "data")
    assert approved == ["files.write"]
    assert (Workspace(tmp_path).dir / "note.txt").read_text() == "data"


def test_llm_worker_session_cap():
    from pyharness.broker.capabilities import LLMCapability

    class StubLLM:
        def complete(self, *, system, messages, tier="cheap", tools=None, max_tokens=None):
            from pyharness.llm.client import Completion

            return Completion(text="ok", tool_calls=[], content=[])

    # In fan-out the cap surfaces as failed Results, not an exception.
    cap = LLMCapability(StubLLM(), session_cap=2)
    results = cap.map_llm(["a", "b", "c"])
    assert sum(r.ok for r in results) == 2
    assert any("cap reached" in (r.error or "") for r in results)

    # The cap counts fan-out workers only; single llm() calls stay uncapped.
    assert cap.run("one more") == "ok"


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
    def __init__(self, status=200, text="ok", url="http://x", content_type="text/plain", content=None):
        import datetime

        self.status_code = status
        self.text = text
        self.url = url
        self.content = content if content is not None else text.encode()
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

    web.fetch("http://x", auth="k")
    assert fake_httpx.instances[-1].calls[-1]["headers"]["Authorization"] == "Bearer S3CRET"

    web.fetch("http://x", auth="k", auth_style="header", auth_name="X-API-Key")
    assert fake_httpx.instances[-1].calls[-1]["headers"]["X-API-Key"] == "S3CRET"

    web.fetch("http://x", auth="k", auth_style="query", auth_name="api_key")
    assert fake_httpx.instances[-1].calls[-1]["params"] == {"api_key": "S3CRET"}

    web.fetch("http://x", auth="k", auth_style="basic", user="alice")
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


# A page carrying every affordance shape: a <base> that reroutes relative links,
# duplicate/javascript/fragment anchors the parser must handle, and a login form
# with a labelled field, a required password, a hidden CSRF token, and a select.
_RICH_HTML = """
<html><head><title>Sign in — Example</title>
<base href="https://example.com/app/"></head>
<body>
  <nav>
    <a href="/pricing">Pricing</a>
    <a href="/pricing">Pricing again</a>
    <a href="https://docs.example.com">Docs</a>
    <a href="javascript:void(0)">JS</a>
    <a href="#top">Top</a>
  </nav>
  <main>
    <h1>Welcome back</h1>
    <p>Please sign in to your account to continue reading the full guide,
       which covers everything you need to get started with the platform.</p>
    <form action="login" method="post" enctype="multipart/form-data">
      <label for="email">Email address</label>
      <input id="email" name="user[email]" type="text" required>
      <input id="pw" name="user[password]" type="password" required>
      <input type="hidden" name="csrf" value="tok-abc123">
      <input type="checkbox" name="remember">
      <select name="plan"><option value="free">Free</option><option value="pro">Pro</option></select>
      <button type="submit">Sign in</button>
    </form>
  </main>
</body></html>
"""


def test_parse_affordances_links_dedup_resolve_and_filter():
    from pyharness.broker.capabilities.page import parse_affordances

    title, links, forms = parse_affordances(_RICH_HTML, "https://example.com/page")
    assert title == "Sign in — Example"
    hrefs = [link["href"] for link in links]
    # <base> reroutes the relative link; absolute survives; duplicate collapses;
    # javascript: and fragment-only anchors are dropped.
    assert "https://example.com/pricing" in hrefs
    assert "https://docs.example.com" in hrefs
    assert hrefs.count("https://example.com/pricing") == 1
    assert not any(h.startswith("javascript:") or "#top" in h for h in hrefs)


def test_parse_affordances_form_fields():
    from pyharness.broker.capabilities.page import parse_affordances

    _, _, forms = parse_affordances(_RICH_HTML, "https://example.com/page")
    assert len(forms) == 1
    form = forms[0]
    assert form["method"] == "POST"
    assert form["action"] == "https://example.com/app/login"  # relative + <base>
    assert form["enctype"] == "multipart/form-data"
    assert form["submit"] == "Sign in"
    fields = {f["name"]: f for f in form["fields"]}
    assert fields["user[email]"]["label"] == "Email address"
    assert fields["user[password]"]["required"] is True
    assert fields["csrf"]["type"] == "hidden" and fields["csrf"]["value"] == "tok-abc123"
    assert fields["plan"]["options"] == ["free", "pro"]
    # the submit button is captured as `submit`, not as a fillable field
    assert "" not in fields


def test_parse_affordances_captures_option_values():
    from pyharness.broker.capabilities.page import parse_affordances

    html = (
        "<form><input type='radio' name='size' value='s'>"
        "<input type='radio' name='size' value='l'>"
        "<input type='text' name='q' value='prefilled'></form>"
    )
    _, _, forms = parse_affordances(html, "http://x")
    values = [f.get("value") for f in forms[0]["fields"]]
    assert values == ["s", "l", "prefilled"]  # radio choices + prefilled default


def test_parse_affordances_select_options_capped():
    from pyharness.broker.capabilities.page import parse_affordances

    options = "".join(f"<option value='c{i}'>C{i}</option>" for i in range(60))
    html = f"<form><select name='country'>{options}</select></form>"
    _, _, forms = parse_affordances(html, "http://x")
    field = forms[0]["fields"][0]
    assert len(field["options"]) == 30
    assert field["options_truncated"] == 30


def test_extract_content_returns_none_on_empty():
    from pyharness.broker.capabilities.page import extract_content

    assert extract_content("") is None
    assert extract_content("<html><body></body></html>") is None


def test_render_page_map_caps_and_omits_empty_sections():
    from pyharness.broker.capabilities.page import render_page_map

    # Plain prose: no FORMS/LINKS headings appear.
    assert render_page_map("Just an article.", [], []) == "Just an article."

    links = [{"text": f"L{i}", "href": f"http://x/{i}"} for i in range(130)]
    mapped = render_page_map("body", links, [], title="T")
    assert mapped.startswith("# T")
    assert "## LINKS" in mapped
    assert "and 30 more links" in mapped  # 130 links, 100 shown


def test_web_fetch_renders_full_page_map(tmp_path, monkeypatch):
    import httpx

    class _Client:
        def __init__(self, **kwargs):
            pass

        def request(self, method, url, **kwargs):
            return _FakeResp(text=_RICH_HTML, url=url, content_type="text/html")

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _Client)
    web = WebCapability(llm=None, http=HttpSessionCapability(Workspace(tmp_path)))
    out = web.fetch("https://example.com/page")

    assert out.startswith("# Sign in — Example")
    assert "## FORMS" in out and "## LINKS" in out
    assert "POST https://example.com/app/login" in out
    assert "user[password]" in out and "required" in out
    assert "https://docs.example.com" in out


def test_web_fetch_spilled_page_keeps_affordances(tmp_path, monkeypatch):
    import httpx

    class _Client:
        def __init__(self, **kwargs):
            pass

        def request(self, method, url, **kwargs):
            return _FakeResp(text=_RICH_HTML, url=url, content_type="text/html")

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _Client)
    web = WebCapability(llm=None, http=HttpSessionCapability(Workspace(tmp_path)))
    # save= forces the body to disk; the affordance map must still ride back.
    out = web.fetch("https://example.com/page", save="page.html")
    assert "saved" in out and "page.html" in out
    assert "## FORMS" in out and "## LINKS" in out
    assert (Workspace(tmp_path).path("page.html")).exists()


def test_web_fetch_falls_back_when_extract_content_declines(tmp_path, monkeypatch):
    import httpx

    import pyharness.broker.capabilities.http as http_mod

    # trafilatura declines (returns None) -> the stdlib reducer must still yield
    # readable content, and the affordance parse is unaffected.
    monkeypatch.setattr(http_mod, "extract_content", lambda html: None)

    class _Client:
        def __init__(self, **kwargs):
            pass

        def request(self, method, url, **kwargs):
            return _FakeResp(text=_RICH_HTML, url=url, content_type="text/html")

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _Client)
    web = WebCapability(llm=None, http=HttpSessionCapability(Workspace(tmp_path)))
    out = web.fetch("https://example.com/page")
    assert "Welcome back" in out  # fallback reducer produced the content
    assert "## LINKS" in out  # affordances unaffected by the content fallback


def test_sink_redacted_recurses_into_lists():
    from pyharness.security.sink import SecretSink

    sink = SecretSink(Vault({"k": "S3CRET"}))
    sink.resolve("k")
    out = sink.redacted({"links": [{"href": "http://x?t=S3CRET", "text": "S3CRET"}]})
    assert out["links"][0]["href"] == "http://x?t=***"
    assert out["links"][0]["text"] == "***"


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
    out = web.fetch("http://x")
    assert "Hello" in out and "world" in out  # content extracted...
    assert "<" not in out and "script" not in out  # ...markup and noise stripped

    # A non-HTML body is returned verbatim, not run through extraction.
    monkeypatch.setattr(httpx, "Client", client_returning("application/json", '{"a": 1}'))
    assert web.fetch("http://x") == '{"a": 1}'


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
    assert r["saved"] is False and r["path"] is None
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


def _serve(monkeypatch, resp):
    """Point httpx.Client at a client that returns `resp` for every request."""
    import httpx

    class _C:
        def __init__(self, **kwargs):
            pass

        def request(self, method, url, **kwargs):
            return resp

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _C)


def test_http_returns_full_body_uncapped(tmp_path, monkeypatch):
    # The G1 fix: a textual body larger than the old 10k display cap now reaches
    # the agent's variable whole, not a truncated head — the design's core promise.
    big = "x" * 50_000
    _serve(monkeypatch, _FakeResp(text=big, content_type="application/json"))
    r = HttpSessionCapability(Workspace(tmp_path)).request(None, "GET", "http://x")
    assert r["saved"] is False and r["path"] is None
    assert r["text"] == big  # full 50k, nothing dropped


def test_http_saves_binary_body_to_workspace(tmp_path, monkeypatch):
    # A binary content-type spills to a workspace file (full bytes intact) instead
    # of returning mojibake text; the agent reads/parses the file with its own lib.
    pdf = b"%PDF-1.7\n" + b"\x00\x01\x02" * 1000
    _serve(monkeypatch, _FakeResp(text="ignored", content_type="application/pdf", content=pdf))
    ws = Workspace(tmp_path)
    r = HttpSessionCapability(ws).request(None, "GET", "http://x/report.pdf")
    assert r["text"] is None and r["saved"] is True
    assert r["bytes"] == len(pdf)
    assert ws.path(r["path"]).read_bytes() == pdf  # full body on disk
    assert r["path"].endswith(".pdf")  # extension guessed from content-type


def test_http_spills_oversized_text_to_workspace(tmp_path, monkeypatch):
    from pyharness.broker.capabilities import payload

    monkeypatch.setattr(payload, "INLINE_TEXT_LIMIT", 100)
    body = "y" * 500
    _serve(monkeypatch, _FakeResp(text=body, content_type="text/plain"))
    ws = Workspace(tmp_path)
    r = HttpSessionCapability(ws).request(None, "GET", "http://x")
    assert r["saved"] is True and r["text"] is None
    assert r["preview"] == body[:payload.PREVIEW_CHARS]
    assert ws.path(r["path"]).read_text() == body  # full text, not the preview


def test_http_explicit_save_writes_full_body_to_named_path(tmp_path, monkeypatch):
    _serve(monkeypatch, _FakeResp(text='{"a": 1}', content_type="application/json"))
    ws = Workspace(tmp_path)
    r = HttpSessionCapability(ws).request(None, "GET", "http://x", save="out.json")
    assert r["saved"] is True and r["path"] == "out.json" and r["text"] is None
    assert ws.path("out.json").read_text() == '{"a": 1}'


def test_http_saved_body_redacts_injected_secret(tmp_path, monkeypatch):
    # A secret echoed in a spilled body must be masked on disk, not just in the
    # returned dict — same use-but-don't-view rule as an inline response.
    echoed = b"binary blob with hunter2 inside"
    _serve(monkeypatch, _FakeResp(text="x", content_type="application/octet-stream", content=echoed))
    ws = Workspace(tmp_path)
    http = HttpSessionCapability(ws, vault=Vault({"pw": "hunter2"}))
    r = http.request(None, "POST", "http://x", json={"u": "me"}, secret_fields={"password": "pw"})
    on_disk = ws.path(r["path"]).read_bytes()
    assert b"hunter2" not in on_disk and b"***" in on_disk


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


# --- TOTP derivation (Direction #6) ------------------------------------------


# RFC 6238 Appendix B vectors: 8 digits, 30s period, T0=0. The seed is the
# ASCII string "1234567890" repeated to the digest's block-appropriate length.
_TOTP_VECTORS = [
    (59, "sha1", "94287082"),
    (1111111109, "sha1", "07081804"),
    (1111111111, "sha1", "14050471"),
    (1234567890, "sha1", "89005924"),
    (2000000000, "sha1", "69279037"),
    (20000000000, "sha1", "65353130"),
    (59, "sha256", "46119246"),
    (59, "sha512", "90693936"),
]


def _rfc_seed(algorithm: str) -> str:
    import base64

    length = {"sha1": 20, "sha256": 32, "sha512": 64}[algorithm]
    raw = ("1234567890" * 7)[:length].encode()
    return base64.b32encode(raw).decode()


def test_totp_rfc6238_vectors():
    from pyharness.security.totp import totp_code

    for at, algorithm, expected in _TOTP_VECTORS:
        assert totp_code(_rfc_seed(algorithm), digits=8, algorithm=algorithm, at=at) == expected


def test_totp_defaults_six_digits_zero_padded():
    from pyharness.security.totp import totp_code

    # Same vector truncated to the 6-digit default; str+zfill keeps a leading zero.
    code = totp_code(_rfc_seed("sha1"), at=59)
    assert code == "287082" and len(code) == 6
    assert totp_code(_rfc_seed("sha1"), at=1111111109) == "081804"


def test_totp_normalizes_provisioning_formatting():
    from pyharness.security.totp import totp_code

    # Provisioning UIs show seeds lowercase, space-grouped, unpadded — all must
    # derive the same code as the canonical form.
    canonical = _rfc_seed("sha1")
    grouped = " ".join(canonical[i : i + 4] for i in range(0, len(canonical), 4)).lower()
    assert totp_code(grouped, at=59) == totp_code(canonical, at=59)


def test_totp_rejects_bad_seed_without_echoing_it():
    from pyharness.security.totp import totp_code

    with pytest.raises(ValueError) as exc:
        totp_code("not!base32", at=59)
    assert "not!base32" not in str(exc.value)  # a seed is credential material
    with pytest.raises(ValueError):
        totp_code("", at=59)
    with pytest.raises(ValueError):
        totp_code(_rfc_seed("sha1"), algorithm="md5", at=59)


def test_secret_sink_tracks_derived_cleartext():
    from pyharness.security.sink import SecretSink

    sink = SecretSink(None)  # track() needs no vault — the value is already cleartext
    sink.track("287082")
    assert sink.has_injected
    assert sink.redact("you entered 287082") == "you entered ***"


# --- Browser lane (C2) -------------------------------------------------------
# Playwright is an optional extra, so these drive the capability against a fake
# page — no chromium binary, runs in CI. A live smoke test would be
# @pytest.mark.skipif(playwright missing) and is intentionally left out of CI.


# A canned aria snapshot (mode="ai" shape) — one ref per line, a link with a url.
# The live `aria-ref=` resolution is proven against real chromium; here the fake
# only needs the ref markers so the capability's routing/validation is exercised.
_FAKE_SNAPSHOT = '- textbox "Email" [ref=e5]\n- button "Submit application" [ref=e6]\n- link "Jobs" [ref=e7]:\n  - /url: /careers'


class _FakePage:
    """Records every action; `_text` is what inner_text returns (used to prove
    injected secrets get masked on read-back); `_snapshot` is the aria tree."""

    def __init__(self, url="http://start", text="page body", snapshot=_FAKE_SNAPSHOT):
        self.url = url
        self._text = text
        self._snapshot = snapshot
        self._wait_timeout = False  # when True, wait_for_selector raises TimeoutError
        self.calls: list = []
        self.keyboard = SimpleNamespace(press=lambda key: self.calls.append(("keyboard.press", key)))
        self.mouse = SimpleNamespace(wheel=lambda dx, dy: self.calls.append(("wheel", dx, dy)))

    def goto(self, url, wait_until=None):
        self.url = url
        self.calls.append(("goto", url, wait_until))
        return SimpleNamespace(status=200)  # playwright Response.status

    def title(self):
        return "Title"

    def aria_snapshot(self, *, mode=None, **kw):
        self.calls.append(("aria_snapshot", mode))
        return self._snapshot

    def click(self, selector, **kw):
        self.calls.append(("click", selector))

    def fill(self, selector, value, **kw):
        self.calls.append(("fill", selector, value))

    def select_option(self, selector, **kw):
        self.calls.append(("select_option", selector, kw))

    def press(self, selector, key, **kw):
        self.calls.append(("press", selector, key))

    def wait_for_selector(self, selector, state=None, timeout=None):
        self.calls.append(("wait_for_selector", selector, state))
        if self._wait_timeout:
            raise TimeoutError("waiting for selector timed out")
        return object()

    def set_input_files(self, selector, files, **kw):
        self.calls.append(("set_input_files", selector, files))

    def inner_text(self, selector):
        return self._text

    def screenshot(self, path=None, *, type=None, quality=None, full_page=False):
        self.calls.append(("screenshot", path, type))
        return b"\xff\xd8fake-jpeg-bytes"  # look() reads the return; screenshot(path=) ignores it


def _browser_with_fake(ws, vault=None, text="page body", snapshot=_FAKE_SNAPSHOT):
    """A BrowserCapability with one fake session injected under id "sid" — skips
    open_browser so no real driver/chromium is launched."""
    from pyharness.broker.capabilities.browser import BrowserCapability, _BrowserSession
    from pyharness.security.sink import SecretSink

    cap = BrowserCapability(ws, vault=vault)
    page = _FakePage(text=text, snapshot=snapshot)
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


def _freeze_totp_time(monkeypatch, at=59):
    # fill_totp derives at "now"; pin the totp module's clock so the expected
    # code is the RFC vector for T=59 ("287082" at 6 digits).
    from types import SimpleNamespace

    monkeypatch.setattr("pyharness.security.totp.time", SimpleNamespace(time=lambda: at))


def test_browser_fill_totp_types_code_never_seed_or_code(tmp_path, monkeypatch):
    _freeze_totp_time(monkeypatch)
    seed = _rfc_seed("sha1")
    cap, page = _browser_with_fake(Workspace(tmp_path), vault=Vault({"github_totp": seed}))
    result = cap.fill_totp("sid", "#otp", "github_totp")
    # The derived code was typed into the page; neither it nor the seed returns.
    assert ("fill", "#otp", "287082") in page.calls
    assert seed not in repr(result) and "287082" not in repr(result)


def test_browser_fill_totp_masks_echoed_code_and_gates_look(tmp_path, monkeypatch):
    _freeze_totp_time(monkeypatch)
    cap, page = _browser_with_fake(
        Workspace(tmp_path), vault=Vault({"github_totp": _rfc_seed("sha1")}), text="code 287082 accepted"
    )
    cap.fill_totp("sid", "#otp", "github_totp")
    r = cap.read_text("sid")
    assert "287082" not in r["text"] and "***" in r["text"]
    # A second factor in the page is a secret on screen: the look gate applies.
    assert cap.has_injected_secrets("sid")


def test_browser_read_text_returns_full_page_and_can_save(tmp_path, monkeypatch):
    from pyharness.broker.capabilities import payload

    page = "p" * 30_000
    cap, _ = _browser_with_fake(Workspace(tmp_path), text=page)
    # Under the ceiling the whole page rides back inline, uncapped.
    r = cap.read_text("sid")
    assert r["saved"] is False and r["text"] == page
    # Past the ceiling it spills to the workspace with the full text on disk.
    monkeypatch.setattr(payload, "INLINE_TEXT_LIMIT", 100)
    ws = Workspace(tmp_path)
    cap2, _ = _browser_with_fake(ws, text=page)
    r2 = cap2.read_text("sid")
    assert r2["saved"] is True and r2["text"] is None
    assert ws.path(r2["path"]).read_text() == page


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


# --- Browser perception: snapshot + refs (Direction #3, PR-1) ----------------


def test_browser_snapshot_returns_refs_and_stores_it(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path))
    r = cap.snapshot("sid")
    assert r["url"] == "http://start" and r["title"] == "Title"
    assert "[ref=e6]" in r["text"] and "/url: /careers" in r["text"]
    # Stored so refs and preview() can resolve against it.
    assert "[ref=e6]" in cap._sessions["sid"].last_snapshot
    assert ("aria_snapshot", "ai") in page.calls


def test_browser_snapshot_masks_injected_secret(tmp_path):
    # A fill_secret value can surface as a textbox value in the aria tree exactly
    # as it can in read_text; it must be masked before it reaches agent code.
    snap = '- textbox "Email" [ref=e5]\n- textbox "Password" [ref=e6]: hunter2'
    cap, _ = _browser_with_fake(Workspace(tmp_path), vault=Vault({"pw": "hunter2"}), snapshot=snap)
    cap.fill_secret("sid", "#password", "pw")
    r = cap.snapshot("sid")
    assert "hunter2" not in r["text"] and "***" in r["text"]


def test_browser_snapshot_can_save(tmp_path, monkeypatch):
    from pyharness.broker.capabilities import payload

    big = "- generic [ref=e1]\n" + "x" * 30_000
    monkeypatch.setattr(payload, "INLINE_TEXT_LIMIT", 100)
    ws = Workspace(tmp_path)
    cap, _ = _browser_with_fake(ws, snapshot=big)
    r = cap.snapshot("sid")
    assert r["saved"] is True and r["text"] is None
    assert ws.path(r["path"]).read_text() == big
    # Even spilled to disk, the ref stays resolvable — stored snapshot is the text.
    assert "[ref=e1]" in cap._sessions["sid"].last_snapshot


def test_browser_click_by_ref_resolves_to_aria_ref(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path))
    cap.snapshot("sid")
    cap.click("sid", ref="e6")
    assert ("click", "aria-ref=e6") in page.calls


def test_browser_fill_by_ref(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path))
    cap.snapshot("sid")
    cap.fill("sid", ref="e5", value="ada@x.com")
    assert ("fill", "aria-ref=e5", "ada@x.com") in page.calls


def test_browser_ref_without_snapshot_fails_fast(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path))
    with pytest.raises(ValueError, match="snapshot"):
        cap.click("sid", ref="e6")
    assert not any(c[0] == "click" for c in page.calls)  # never reached Playwright


def test_browser_unknown_ref_fails_fast(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path))
    cap.snapshot("sid")
    with pytest.raises(ValueError, match="not in the current snapshot"):
        cap.click("sid", ref="e9")
    assert not any(c[0] == "click" for c in page.calls)


def test_browser_ref_substring_is_not_a_false_match(tmp_path):
    # "[ref=e1]" must not match a snapshot that only contains "[ref=e12]".
    cap, _ = _browser_with_fake(Workspace(tmp_path), snapshot="- button [ref=e12]")
    cap.snapshot("sid")
    with pytest.raises(ValueError, match="not in the current snapshot"):
        cap.click("sid", ref="e1")
    cap.click("sid", ref="e12")  # the real ref resolves


def test_browser_selector_and_ref_mutually_exclusive(tmp_path):
    cap, _ = _browser_with_fake(Workspace(tmp_path))
    cap.snapshot("sid")
    with pytest.raises(ValueError, match="exactly one"):
        cap.click("sid", "#go", ref="e6")
    with pytest.raises(ValueError, match="exactly one"):
        cap.click("sid")  # neither


def test_browser_goto_invalidates_refs(tmp_path):
    cap, _ = _browser_with_fake(Workspace(tmp_path))
    cap.snapshot("sid")
    cap.goto("sid", "http://elsewhere")  # navigation clears the snapshot
    with pytest.raises(ValueError, match="snapshot"):
        cap.click("sid", ref="e6")


def test_browser_preview_shows_ref_element(tmp_path):
    cap, _ = _browser_with_fake(Workspace(tmp_path))
    cap.snapshot("sid")
    cat, summary = cap.preview("click", ("sid",), {"ref": "e6"})
    assert cat is ActionCategory.OUTWARD
    assert "[ref=e6]" in summary and "Submit application" in summary


# --- Browser verbs: select / press / scroll / wait / upload (Direction #3, PR-2) --


def test_browser_select_option_by_ref(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path))
    cap.snapshot("sid")
    cap.select_option("sid", ref="e6", label="Remote")
    assert ("select_option", "aria-ref=e6", {"label": "Remote"}) in page.calls


def test_browser_press_targets_element_or_focused(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path))
    cap.snapshot("sid")
    cap.press("sid", "Enter", ref="e6")
    assert ("press", "aria-ref=e6", "Enter") in page.calls
    cap.press("sid", "Tab")  # no target -> goes to the focused element
    assert ("keyboard.press", "Tab") in page.calls


def test_browser_scroll_uses_wheel(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path))
    cap.scroll("sid", 400)
    assert ("wheel", 0, 400) in page.calls


def test_browser_wait_for_found_and_timeout(tmp_path):
    cap, page = _browser_with_fake(Workspace(tmp_path))
    assert cap.wait_for("sid", "#ready")["found"] is True
    page._wait_timeout = True  # a real Playwright TimeoutError becomes a clean False
    assert cap.wait_for("sid", "#never")["found"] is False


def test_browser_upload_stages_workspace_file(tmp_path):
    ws = Workspace(tmp_path)
    cap, page = _browser_with_fake(ws)
    cap.snapshot("sid")
    r = cap.upload("sid", "resume.pdf", ref="e5")
    call = next(c for c in page.calls if c[0] == "set_input_files")
    assert call[1] == "aria-ref=e5" and call[2] == str(ws.path("resume.pdf"))
    assert r["uploaded"] == "resume.pdf"


def test_browser_upload_rejects_workspace_escape(tmp_path):
    cap, _ = _browser_with_fake(Workspace(tmp_path))
    with pytest.raises(ValueError):
        cap.upload("sid", "../secret.pdf", "#f")


def test_browser_g9_verb_gating(tmp_path):
    from pyharness.broker.capabilities.browser import MUTATING_ACTIONS

    prompted = []

    def approver(request):
        prompted.append(request.action)
        return True  # allow, so the fake actions proceed

    cap, _ = _browser_with_fake(Workspace(tmp_path))
    broker = _broker(tmp_path, policy=Policy(require_approval=set(MUTATING_ACTIONS)), approver=approver)
    broker.register(cap)
    ns = broker.namespace()

    ns["scroll"]("sid", 200)  # viewport-only: free
    ns["wait_for"]("sid", "#x")  # read: free
    assert prompted == []

    ns["select_option"]("sid", "#s", value="a")
    ns["press"]("sid", "Enter", "#f")
    ns["upload"]("sid", "cv.pdf", "#u")
    assert prompted == ["browser.select_option", "browser.press", "browser.upload"]


def test_browser_upload_preview_names_the_file(tmp_path):
    cap, _ = _browser_with_fake(Workspace(tmp_path))
    cat, summary = cap.preview("upload", ("sid", "resume.pdf"), {"selector": "#f"})
    assert cat is ActionCategory.OUTWARD
    assert "resume.pdf" in summary and "#f" in summary


# --- Browser image path: look + MediaOutbox (Direction #3, PR-3) -------------


def test_media_outbox_attach_drain_and_caps():
    from pyharness.core.media import MediaOutbox

    box = MediaOutbox(max_bytes=100, max_items=2)
    box.attach(media_type="image/jpeg", data=b"abc")
    blocks = box.drain()
    assert len(blocks) == 1 and blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(blocks[0]["source"]["data"]) == b"abc"
    assert box.drain() == []  # drained empties the buffer

    box.attach(media_type="image/jpeg", data=b"x")
    box.attach(media_type="image/jpeg", data=b"y")
    with pytest.raises(ValueError):  # over the per-cell count
        box.attach(media_type="image/jpeg", data=b"z")
    box.drain()
    with pytest.raises(ValueError):  # over the per-image byte cap
        box.attach(media_type="image/jpeg", data=b"x" * 101)


def test_browser_look_stages_image_and_returns_no_bytes(tmp_path):
    from pyharness.core.media import MediaOutbox

    box = MediaOutbox()
    cap, _ = _browser_with_fake(Workspace(tmp_path))
    cap.media = box
    r = cap.look("sid")
    assert r["attached"] is True and r["bytes"] == len(b"\xff\xd8fake-jpeg-bytes")
    # The raw bytes are staged for the agent loop, never returned to agent code.
    assert "fake-jpeg-bytes" not in repr(r)
    blocks = box.drain()
    assert len(blocks) == 1 and blocks[0]["source"]["media_type"] == "image/jpeg"


def test_browser_look_without_media_channel_raises(tmp_path):
    cap, _ = _browser_with_fake(Workspace(tmp_path))  # no media wired
    with pytest.raises(RuntimeError, match="no image channel"):
        cap.look("sid")


def test_browser_look_gated_only_after_secret_injected(tmp_path):
    # Models the default-policy predicate: look is free until a secret is typed
    # into the page, then it needs approval (pixels can carry the secret).
    cap, _ = _browser_with_fake(Workspace(tmp_path), vault=Vault({"pw": "hunter2"}))
    assert cap.has_injected_secrets("sid") is False
    cap.fill_secret("sid", "#password", "pw")
    assert cap.has_injected_secrets("sid") is True


# --- Site profiles: persistent web identity (G3, Direction #5) ---------------
# Cookie material is credential-grade, so these prove the two invariants that
# matter: the cleartext storage_state never lands on disk unencrypted, and agent
# code only ever sees a name/counts, never the cookies. No chromium — a fake
# driver stands in for the storage_state round-trip.

_SENTINEL_STATE = {
    "cookies": [{"name": "sid", "value": "TOP-SECRET-COOKIE", "domain": "linkedin.com", "path": "/"}],
    "origins": [{"origin": "https://linkedin.com", "localStorage": []}],
}


class _FakeProfileContext:
    """A browser context that yields a canned storage_state on save."""

    def __init__(self, state):
        self._state = state
        self.storage_state_calls: list = []

    def new_page(self):
        return _FakePage()

    def storage_state(self, indexed_db=None):
        self.storage_state_calls.append(indexed_db)
        return self._state

    def close(self):
        pass


class _FakeProfileBrowser:
    def __init__(self, save_state):
        self._save_state = save_state
        self.opened_with = "UNSET"  # records new_context(storage_state=...)

    def new_context(self, storage_state=None):
        self.opened_with = storage_state
        # After a restore, the context reports back the state it was opened with
        # (an empty session reports the canned save_state).
        return _FakeProfileContext(storage_state or self._save_state)

    def close(self):
        pass


def _fake_driver(save_state=None):
    browser = _FakeProfileBrowser(save_state or {"cookies": [], "origins": []})
    return SimpleNamespace(chromium=SimpleNamespace(launch=lambda headless=True: browser)), browser


def _store(tmp_path):
    from pyharness.security.profiles import ProfileStore

    return ProfileStore(tmp_path / "profiles", "test-pass")


def test_profile_store_round_trip_and_wrong_passphrase(tmp_path):
    from pyharness.security.profiles import ProfileStore

    store = _store(tmp_path)
    store.save("linkedin", _SENTINEL_STATE)
    assert store.load("linkedin") == _SENTINEL_STATE
    assert store.names() == ["linkedin"]
    assert store.exists("linkedin") and not store.exists("google")
    assert oct((tmp_path / "profiles" / "linkedin.enc").stat().st_mode)[-3:] == "600"

    with pytest.raises(Exception):  # noqa: PT011 - Fernet InvalidToken, any auth failure
        ProfileStore(tmp_path / "profiles", "wrong-pass").load("linkedin")

    store.delete("linkedin")
    assert store.names() == []


def test_profile_store_rejects_bad_names(tmp_path):
    store = _store(tmp_path)
    for bad in ("../escape", "a/b", "", "A B", "has.dot"):
        with pytest.raises(ValueError):
            store.save(bad, _SENTINEL_STATE)
        with pytest.raises(ValueError):
            store.load(bad)


def test_profile_cleartext_never_on_disk(tmp_path):
    store = _store(tmp_path)
    store.save("linkedin", _SENTINEL_STATE)
    for path in (tmp_path / "profiles").rglob("*"):
        if path.is_file():
            assert b"TOP-SECRET-COOKIE" not in path.read_bytes()


def test_profile_store_from_env_fails_closed_without_passphrase(tmp_path, monkeypatch):
    from pyharness.security.profiles import ProfileStore

    monkeypatch.delenv("PYHARNESS_VAULT_PASSPHRASE", raising=False)
    assert ProfileStore.from_env() is None


def test_open_browser_with_profile_restores_state(tmp_path):
    from pyharness.broker.capabilities.browser import BrowserCapability

    store = _store(tmp_path)
    store.save("linkedin", _SENTINEL_STATE)
    cap = BrowserCapability(Workspace(tmp_path), profiles=store)
    driver, browser = _fake_driver()
    cap._pw = driver

    sid = cap.open_browser(profile="linkedin")
    assert browser.opened_with == _SENTINEL_STATE  # restored into new_context
    assert cap._sessions[sid].profile == "linkedin"
    assert isinstance(sid, str) and len(sid) == 32  # a bare session id, no cookies


def test_open_browser_plain_does_not_touch_profiles(tmp_path):
    from pyharness.broker.capabilities.browser import BrowserCapability

    cap = BrowserCapability(Workspace(tmp_path), profiles=None)  # no store configured
    driver, browser = _fake_driver()
    cap._pw = driver

    sid = cap.open_browser()  # plain open works with no passphrase
    assert cap._sessions[sid].profile is None
    assert browser.opened_with is None


def test_open_browser_profile_fails_closed(tmp_path):
    from pyharness.broker.capabilities.browser import BrowserCapability

    cap = BrowserCapability(Workspace(tmp_path), profiles=None)
    cap._pw, _ = _fake_driver()
    with pytest.raises(RuntimeError, match="PYHARNESS_VAULT_PASSPHRASE"):
        cap.open_browser(profile="linkedin")

    cap2 = BrowserCapability(Workspace(tmp_path), profiles=_store(tmp_path))
    cap2._pw, _ = _fake_driver()
    with pytest.raises(KeyError):  # unknown profile
        cap2.open_browser(profile="nope")


def _profile_session(cap, profile="linkedin", state=_SENTINEL_STATE):
    """Inject a session whose context reports `state` on save, tagged `profile`."""
    from pyharness.broker.capabilities.browser import _BrowserSession
    from pyharness.security.sink import SecretSink

    ctx = _FakeProfileContext(state)
    cap._sessions["sid"] = _BrowserSession(
        browser=_FakeClient(), context=ctx, page=_FakePage(), sink=SecretSink(None), profile=profile
    )
    return ctx


def test_save_profile_returns_counts_never_values(tmp_path):
    from pyharness.broker.capabilities.browser import BrowserCapability

    store = _store(tmp_path)
    cap = BrowserCapability(Workspace(tmp_path), profiles=store)
    ctx = _profile_session(cap, profile=None)  # not yet a named profile

    result = cap.save_profile("sid", "linkedin")
    assert result == {"profile": "linkedin", "cookies": 1, "origins": 1}
    assert "TOP-SECRET-COOKIE" not in repr(result)
    assert True in ctx.storage_state_calls  # indexed_db=True passed on save
    assert store.load("linkedin") == _SENTINEL_STATE
    assert cap._sessions["sid"].profile == "linkedin"  # now refreshes on close


def test_list_profiles_returns_names_only(tmp_path):
    from pyharness.broker.capabilities.browser import BrowserCapability

    store = _store(tmp_path)
    store.save("linkedin", _SENTINEL_STATE)
    cap = BrowserCapability(Workspace(tmp_path), profiles=store)
    assert cap.list_profiles() == ["linkedin"]
    assert BrowserCapability(Workspace(tmp_path), profiles=None).list_profiles() == []


def test_close_refreshes_profile_and_audits(tmp_path):
    import json

    from pyharness.broker.capabilities.browser import BrowserCapability

    store = _store(tmp_path)
    store.save("linkedin", {"cookies": [], "origins": []})  # stale
    audit = AuditLog(tmp_path / "audit.jsonl")
    cap = BrowserCapability(Workspace(tmp_path), profiles=store, audit=audit)
    _profile_session(cap, profile="linkedin")  # context now holds fresh _SENTINEL_STATE

    cap.close_browser("sid")
    assert store.load("linkedin") == _SENTINEL_STATE  # rotated state persisted
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().strip().splitlines()]
    events = [e for e in entries if e.get("event") == "profile_saved"]
    assert events and events[0]["profile"] == "linkedin" and events[0]["trigger"] == "close"
    from pyharness.audit import verify_chain

    assert verify_chain(tmp_path / "audit.jsonl")[0]  # the close-time event stays in the hash chain


def test_close_all_only_refreshes_profile_sessions(tmp_path):
    from pyharness.broker.capabilities.browser import BrowserCapability

    store = _store(tmp_path)
    cap = BrowserCapability(Workspace(tmp_path), profiles=store)
    _profile_session(cap, profile=None)  # a plain session

    cap.close_all()
    assert store.names() == []  # plain session saved nothing


def test_profile_ops_gating(tmp_path):
    from pyharness.broker.capabilities.browser import MUTATING_ACTIONS, BrowserCapability
    from pyharness.core.session import _opens_with_profile

    store = _store(tmp_path)
    store.save("linkedin", _SENTINEL_STATE)
    prompted = []

    def approver(request):
        prompted.append(request.action)
        return True

    cap = BrowserCapability(Workspace(tmp_path), profiles=store)
    cap._pw, _ = _fake_driver()
    policy = Policy(require_approval={"browser.save_profile"}, approve_if=[_opens_with_profile])
    broker = _broker(tmp_path, policy=policy, approver=approver)
    broker.register(cap)
    ns = broker.namespace()

    ns["open_browser"]()  # plain open: free
    ns["list_profiles"]()  # names only: free
    assert prompted == []

    ns["open_browser"](profile="linkedin")  # restores an identity: gated
    _profile_session(cap)
    ns["save_profile"]("sid", "linkedin")  # persists a credential: gated
    assert prompted == ["browser.open_browser", "browser.save_profile"]

    # Neither op is grant-coverable: scope() must yield None for both.
    assert "browser.open_browser" not in MUTATING_ACTIONS
    assert cap.scope("open_browser", (), {"profile": "linkedin"}) is None
    assert cap.scope("save_profile", ("sid", "linkedin"), {}) is None


def test_open_browser_profile_preview_names_profile_and_domains(tmp_path):
    from pyharness.broker.capabilities.browser import BrowserCapability

    store = _store(tmp_path)
    store.save("linkedin", _SENTINEL_STATE)
    cap = BrowserCapability(Workspace(tmp_path), profiles=store)
    cat, summary = cap.preview("open_browser", (), {"profile": "linkedin"})
    assert cat is ActionCategory.OUTWARD
    assert "linkedin" in summary and "linkedin.com" in summary
    # A missing profile must not make the preview raise.
    _, summary2 = cap.preview("open_browser", (), {"profile": "ghost"})
    assert "ghost" in summary2


def test_encrypted_file_save_is_atomic_and_0600(tmp_path):
    from pyharness.security.vault import EncryptedFile

    path = tmp_path / "sub" / "secrets.enc"
    ef = EncryptedFile(path, "pw")
    ef.save({"a": "b"})
    assert ef.load() == {"a": "b"}
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert list((tmp_path / "sub").glob("*.tmp")) == []  # no stray temp file


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
    # environment. The subprocess gets the minimal allowlist environment
    # (security/env.py), so a command like `echo $SECRET` can't read a secret
    # the agent only knows by name — nor any other var not on the allowlist.
    from pyharness.broker.capabilities import ShellCapability

    monkeypatch.setenv("PYHARNESS_SECRET_TOKEN", "supersecret")
    monkeypatch.setenv("PYHARNESS_VAULT_PASSPHRASE", "hunter2")
    monkeypatch.setenv("NOT_LISTED", "dropped")  # default-deny: unknown vars go too
    shell = ShellCapability(Workspace(tmp_path))
    out = shell.bash('echo "tok=$PYHARNESS_SECRET_TOKEN pass=$PYHARNESS_VAULT_PASSPHRASE x=$NOT_LISTED home=$HOME"')
    assert "supersecret" not in out
    assert "hunter2" not in out
    assert "dropped" not in out
    assert "home=/" in out  # the allowlisted basics survive


# --- web.search_results (Exa raw-results search) ---------------------------


class _FakeExaClient:
    """A one-shot httpx.Client stand-in for the Exa POST: records the call and
    returns a canned response. `bodies`/`status` are set per test on the class."""

    body = {"results": []}
    status = 200
    calls: list = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs

    def request(self, method, url, **kwargs):
        _FakeExaClient.calls.append({"method": method, "url": url, "init": self.init_kwargs, **kwargs})
        return SimpleNamespace(
            status_code=type(self).status,
            json=lambda: type(self).body,
            text="error-body-no-key",
        )

    def close(self):
        pass


def _patch_exa(monkeypatch, *, body=None, status=200):
    import httpx

    _FakeExaClient.calls = []
    _FakeExaClient.body = {"results": []} if body is None else body
    _FakeExaClient.status = status
    monkeypatch.setattr(httpx, "Client", _FakeExaClient)
    return _FakeExaClient


def test_web_search_results_queries_exa_and_parses(monkeypatch):
    fake = _patch_exa(monkeypatch, body={"results": [
        {"title": "T1", "url": "https://a.example", "publishedDate": "2026-01-01",
         "author": "Ann", "score": 0.9, "highlights": ["snip one", "snip two"]},
    ]})
    monkeypatch.setenv("EXA_API_KEY", "EXA-SECRET")
    web = WebCapability(llm=None, http=None)

    out = web.search_results("harrington jackets", num_results=5)

    call = fake.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.exa.ai/search"
    assert call["headers"] == {"x-api-key": "EXA-SECRET"}
    assert call["json"] == {
        "query": "harrington jackets", "numResults": 5, "type": "auto",
        "contents": {"highlights": True},
    }
    assert call["init"] == {"timeout": 30}  # Exa's own short HTTP timeout
    assert out == [{
        "title": "T1", "url": "https://a.example", "snippet": "snip one",
        "published_date": "2026-01-01", "author": "Ann", "score": 0.9,
    }]
    assert "EXA-SECRET" not in repr(out)  # the key never rides back on the result


def test_web_search_results_clamps_num_results(monkeypatch):
    fake = _patch_exa(monkeypatch)
    monkeypatch.setenv("EXA_API_KEY", "k")
    web = WebCapability(llm=None, http=None)

    web.search_results("q", num_results=0)
    web.search_results("q", num_results=500)

    assert [c["json"]["numResults"] for c in fake.calls] == [1, 100]


def test_web_search_results_tolerates_sparse_and_empty(monkeypatch):
    # A result missing highlights/score/date/author parses to None/"" (no KeyError),
    # and a response with no "results" key yields [].
    _patch_exa(monkeypatch, body={"results": [{"url": "https://x.example"}]})
    monkeypatch.setenv("EXA_API_KEY", "k")
    web = WebCapability(llm=None, http=None)
    assert web.search_results("q") == [{
        "title": None, "url": "https://x.example", "snippet": "",
        "published_date": None, "author": None, "score": None,
    }]

    _patch_exa(monkeypatch, body={})
    assert web.search_results("q") == []


def test_web_search_results_raises_without_leaking_key(monkeypatch):
    _patch_exa(monkeypatch, status=429)
    monkeypatch.setenv("EXA_API_KEY", "EXA-SECRET")
    web = WebCapability(llm=None, http=None)

    with pytest.raises(RuntimeError) as exc:
        web.search_results("q")
    assert "429" in str(exc.value)
    assert "EXA-SECRET" not in str(exc.value)


def test_web_search_results_requires_key(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    web = WebCapability(llm=None, http=None)
    with pytest.raises(RuntimeError, match="EXA_API_KEY not set"):
        web.search_results("q")


def test_exa_key_is_scrubbed_from_child():
    # Provider keys held parent-side are stripped from the sandboxed child; the Exa
    # key must ride the same list so agent code can't read it via os.environ/printenv.
    from pyharness.llm.client import PROVIDER_SECRET_ENV

    assert "EXA_API_KEY" in PROVIDER_SECRET_ENV


# --- Scoped approval grants (G7, Direction #4) -------------------------------


def test_grant_ledger_exact_match_and_expiry():
    from pyharness.security import GrantLedger, GrantScope

    ledger = GrantLedger()
    scope = GrantScope("browser", "a.com")
    g = ledger.add(scope)
    assert ledger.find(scope) is g
    # Exact-match only: different host, subdomain, or action-class never matches.
    assert ledger.find(GrantScope("browser", "b.a.com")) is None
    assert ledger.find(GrantScope("http", "a.com")) is None
    # An already-expired grant is never returned (and gets pruned).
    ledger.add(GrantScope("http", "x.com"), ttl_s=-1)
    assert ledger.find(GrantScope("http", "x.com")) is None
    assert ledger.active() == [g]
    # Revoke removes it.
    assert ledger.revoke(g.id) is g
    assert ledger.find(scope) is None


def test_bool_approver_still_normalizes(tmp_path):
    # A simple approver returning a bare bool keeps working: True -> allow once,
    # False -> deny. This is the regression guard for the contract change.
    broker = _broker(tmp_path, policy=Policy(require_approval={"files.write"}),
                     approver=lambda r: True)
    broker.register(FilesCapability(Workspace(tmp_path)))
    broker.namespace()["write"]("a.txt", "x")  # allowed once
    assert (Workspace(tmp_path).dir / "a.txt").read_text() == "x"

    broker2 = _broker(tmp_path, policy=Policy(require_approval={"files.write"}),
                      approver=lambda r: False)
    broker2.register(FilesCapability(Workspace(tmp_path)))
    with pytest.raises(PermissionDenied):
        broker2.namespace()["write"]("b.txt", "y")


def test_scope_none_falls_back_to_per_call(tmp_path):
    from pyharness.broker import ApprovalOutcome

    # files.write has no scope() hook -> request.scope is None -> not grantable.
    # Even a GRANT outcome behaves as allow-once: the next call prompts again.
    prompts = []

    def approver(request):
        prompts.append(request.action)
        assert request.scope is None
        return ApprovalOutcome.GRANT

    broker = _broker(tmp_path, policy=Policy(require_approval={"files.write"}), approver=approver)
    broker.register(FilesCapability(Workspace(tmp_path)))
    ns = broker.namespace()
    ns["write"]("a.txt", "x")
    ns["write"]("b.txt", "y")
    assert prompts == ["files.write", "files.write"]  # prompted both times, nothing minted
    # No grant was recorded in the audit chain.
    import json

    entries = [json.loads(line) for line in
               (tmp_path / "audit.jsonl").read_text().strip().splitlines()]
    assert all("grant" not in e for e in entries)


def test_http_and_browser_scope_extraction(tmp_path):
    from pyharness.security import GrantScope

    http = HttpSessionCapability(Workspace(tmp_path))
    # Host parsed from positional or keyword args, lowercased; only non-DELETE
    # mutating methods are grantable.
    assert http.scope("request", ("sid", "POST", "http://API.Example.com/x"), {}) == \
        GrantScope("http", "api.example.com")
    assert http.scope("request", (), {"method": "PUT", "url": "https://h.com/y"}) == \
        GrantScope("http", "h.com")
    assert http.scope("request", ("sid", "DELETE", "http://h.com"), {}) is None  # irreversible
    assert http.scope("request", ("sid", "GET", "http://h.com"), {}) is None  # read
    assert http.scope("request", ("sid", "POST", "not a url"), {}) is None  # unparseable

    cap, _ = _browser_with_fake(Workspace(tmp_path))  # fake page url is http://start
    assert cap.scope("click", ("sid",), {}) == GrantScope("browser", "start")
    assert cap.scope("fill_secret", ("sid",), {}) is None  # credentials always prompt
    assert cap.scope("fill_totp", ("sid",), {}) is None  # a second factor is a credential too
    assert cap.scope("goto", ("sid",), {}) is None  # navigation is not a mutation
    assert cap.scope("click", ("nope",), {}) is None  # no such session


def test_grant_covers_repeat_browser_actions(tmp_path):
    from pyharness.audit import verify_chain
    from pyharness.broker import ApprovalOutcome
    from pyharness.broker.capabilities.browser import MUTATING_ACTIONS

    prompts = []

    def approver(request):
        prompts.append(request.action)
        return ApprovalOutcome.GRANT

    cap, _ = _browser_with_fake(Workspace(tmp_path))
    broker = _broker(tmp_path, policy=Policy(require_approval=set(MUTATING_ACTIONS)), approver=approver)
    broker.register(cap)
    ns = broker.namespace()
    ns["click"]("sid", "#a")  # prompts once, mints a browser grant for host "start"
    ns["fill"]("sid", "#b", "x")  # covered by the grant — no prompt
    assert prompts == ["browser.click"]

    import json

    entries = [json.loads(line) for line in
               (tmp_path / "audit.jsonl").read_text().strip().splitlines()]
    assert sum("grant" in e for e in entries) == 1  # one mint
    assert sum(bool(e.get("grant_id")) for e in entries) == 1  # one covered call
    ok, _ = verify_chain(tmp_path / "audit.jsonl")
    assert ok  # the mint/coverage entries keep the hash chain intact


def test_grant_scoped_to_host(tmp_path):
    from pyharness.broker import ApprovalOutcome
    from pyharness.broker.capabilities.browser import MUTATING_ACTIONS

    seen = []

    def approver(request):
        seen.append(request.scope.target if request.scope else None)
        return ApprovalOutcome.GRANT

    cap, page = _browser_with_fake(Workspace(tmp_path))
    broker = _broker(tmp_path, policy=Policy(require_approval=set(MUTATING_ACTIONS)), approver=approver)
    broker.register(cap)
    ns = broker.namespace()
    ns["click"]("sid", "#a")  # host "start"
    page.url = "http://other.example.com/x"  # the page navigated elsewhere
    ns["fill"]("sid", "#b", "y")  # different host -> the grant does not cover it
    assert seen == ["start", "other.example.com"]


def test_delete_never_covered_by_grant(tmp_path, fake_httpx):
    from pyharness.broker import ApprovalOutcome
    from pyharness.core.session import _is_mutating_http

    prompts = []

    def approver(request):
        prompts.append((request.args[1], request.scope))
        return ApprovalOutcome.GRANT

    broker = _broker(tmp_path, policy=Policy(approve_if=[_is_mutating_http]), approver=approver)
    broker.register(HttpSessionCapability(Workspace(tmp_path)))
    ns = broker.namespace()
    ns["request"](None, "POST", "http://api.x.com/a")  # mints an http grant for api.x.com
    ns["request"](None, "POST", "http://api.x.com/b")  # covered — no prompt
    ns["request"](None, "DELETE", "http://api.x.com/c")  # irreversible — prompts despite the grant
    assert [p[0] for p in prompts] == ["POST", "DELETE"]
    assert prompts[1][1] is None  # DELETE has no grantable scope

    import json

    entries = [json.loads(line) for line in
               (tmp_path / "audit.jsonl").read_text().strip().splitlines()]
    assert sum("grant" in e for e in entries) == 1  # only the POST minted; the DELETE's GRANT minted nothing


def test_fill_secret_not_covered_by_browser_grant(tmp_path):
    from pyharness.broker import ApprovalOutcome
    from pyharness.broker.capabilities.browser import MUTATING_ACTIONS

    prompts = []

    def approver(request):
        prompts.append(request.action)
        return ApprovalOutcome.GRANT

    cap, _ = _browser_with_fake(Workspace(tmp_path), vault=Vault({"pw": "hunter2"}))
    broker = _broker(tmp_path, policy=Policy(require_approval=set(MUTATING_ACTIONS)), approver=approver)
    broker.register(cap)
    ns = broker.namespace()
    ns["click"]("sid", "#a")  # mints a browser grant
    ns["fill_secret"]("sid", "#pw", "pw")  # credential release still prompts
    assert prompts == ["browser.click", "browser.fill_secret"]


def test_fill_totp_not_covered_by_browser_grant(tmp_path, monkeypatch):
    # Releasing a second factor is a credential release: a domain grant covers
    # mechanical actions, never fill_totp — it prompts every time.
    from pyharness.broker import ApprovalOutcome
    from pyharness.broker.capabilities.browser import MUTATING_ACTIONS

    _freeze_totp_time(monkeypatch)
    prompts = []

    def approver(request):
        prompts.append(request.action)
        return ApprovalOutcome.GRANT

    cap, _ = _browser_with_fake(Workspace(tmp_path), vault=Vault({"github_totp": _rfc_seed("sha1")}))
    broker = _broker(tmp_path, policy=Policy(require_approval=set(MUTATING_ACTIONS)), approver=approver)
    broker.register(cap)
    ns = broker.namespace()
    ns["click"]("sid", "#a")  # mints a browser grant
    ns["fill_totp"]("sid", "#otp", "github_totp")  # still prompts
    ns["fill_totp"]("sid", "#otp", "github_totp")  # and prompts again — never minted
    assert prompts == ["browser.click", "browser.fill_totp", "browser.fill_totp"]


def test_look_not_covered_by_browser_grant(tmp_path):
    # A browser-class grant must not silence the secret-gated look() — pixels can
    # carry a credential into model context, so look always prompts.
    from pyharness.broker import ApprovalOutcome
    from pyharness.broker.capabilities.browser import MUTATING_ACTIONS

    prompts = []

    def approver(request):
        prompts.append(request.action)
        return ApprovalOutcome.DENY if request.action == "browser.look" else ApprovalOutcome.GRANT

    cap, _ = _browser_with_fake(Workspace(tmp_path))
    pol = Policy(require_approval=set(MUTATING_ACTIONS),
                 approve_if=[lambda a, ar, kw: a == "browser.look"])
    broker = _broker(tmp_path, policy=pol, approver=approver)
    broker.register(cap)
    ns = broker.namespace()
    ns["click"]("sid", "#a")  # mints a browser grant
    with pytest.raises(PermissionDenied):
        ns["look"]("sid")  # gated, not covered by the grant -> prompted (and denied here)
    assert prompts == ["browser.click", "browser.look"]
    assert cap.scope("look", ("sid",), {}) is None


def test_cli_approve_offers_grant_when_scoped(monkeypatch, capsys):
    from pyharness.broker import ApprovalOutcome, ApprovalRequest
    from pyharness.cli.main import _approve
    from pyharness.security import GrantScope

    req = ApprovalRequest("browser.click", ActionCategory.OUTWARD, "click #x on http://h",
                          ("sid",), {}, GrantScope("browser", "boards.greenhouse.com"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "a")
    assert _approve(req) is ApprovalOutcome.GRANT
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert _approve(req) is ApprovalOutcome.ONCE
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert _approve(req) is ApprovalOutcome.DENY
    # The [a] label is harness-derived from the scope (class name + host).
    out = capsys.readouterr().out
    assert "boards.greenhouse.com" in out and "state-changing browser actions" in out


def test_cli_approve_no_grant_for_irreversible_or_unscoped(monkeypatch):
    from pyharness.broker import ApprovalOutcome, ApprovalRequest
    from pyharness.cli.main import _approve
    from pyharness.security import GrantScope

    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return "a"  # even typing 'a', an unscoped/irreversible call can never grant

    monkeypatch.setattr("builtins.input", fake_input)
    # Irreversible: no [a] offered; 'a' is not 'y' -> DENY.
    irr = ApprovalRequest("http.request", ActionCategory.IRREVERSIBLE, "DELETE http://h",
                          ("sid", "DELETE", "http://h"), {}, GrantScope("http", "h"))
    assert _approve(irr) is ApprovalOutcome.DENY
    # Unscoped (scope None): plain y/N.
    uns = ApprovalRequest("files.write", ActionCategory.OUTWARD, "write x", ("x",), {}, None)
    assert _approve(uns) is ApprovalOutcome.DENY
    assert all("[y/a/N]" not in p for p in prompts)  # the grant prompt is never shown


# --- Notify (direction-8) ---


def test_notify_emits_event_desktop_and_audits(tmp_path):
    from pyharness.broker.capabilities import NotifyCapability

    events, shown = [], []
    broker = _broker(tmp_path)
    broker.register(NotifyCapability(
        on_event=lambda kind, text, **extra: events.append((kind, text, extra)),
        desktop=shown.append,
    ))
    assert broker.namespace()["notify"]("checkpoint saved", level="attention") == "delivered"
    assert events == [("notify", "checkpoint saved", {"level": "attention"})]
    assert shown == ["checkpoint saved"]
    entry = broker.audit.tail(1)[0]
    assert entry["action"] == "notify.notify" and entry["ok"] is True


def test_notify_rejects_unknown_level(tmp_path):
    from pyharness.broker.capabilities import NotifyCapability

    broker = _broker(tmp_path)
    broker.register(NotifyCapability(desktop=None))
    with pytest.raises(ValueError, match="level"):
        broker.namespace()["notify"]("hi", level="urgent")
    entry = broker.audit.tail(1)[0]
    assert entry["action"] == "notify.notify" and entry["ok"] is False


def test_notify_desktop_is_best_effort_and_capped(tmp_path):
    from pyharness.broker.capabilities import NotifyCapability
    from pyharness.broker.capabilities.notify import _BODY_LIMIT

    def boom(message):
        raise RuntimeError("no display")

    # A failing display helper never breaks the agent's call...
    assert NotifyCapability(desktop=boom).notify("still fine") == "delivered"
    # ...and the desktop body is capped while the event keeps the full message.
    events, shown = [], []
    cap = NotifyCapability(
        on_event=lambda kind, text, **extra: events.append(text), desktop=shown.append
    )
    cap.notify("x" * 1000)
    assert len(shown[0]) == _BODY_LIMIT and len(events[0]) == 1000


def test_notify_is_core_builtin_wired_to_session_events(tmp_path):
    from pyharness.core.session import Session

    events = []
    session = Session(tmp_path, on_event=lambda kind, text: events.append((kind, text)))
    try:
        # No real desktop popups from the test suite.
        session.broker._capabilities["notify"].desktop = None
        assert "notify" in session.broker.op_names()
        session.broker.call("notify", "notify", "hello from the agent")
        assert ("notify", "hello from the agent") in events
        trace = (session.workspace.root / "trace.jsonl").read_text()
        assert "hello from the agent" in trace
    finally:
        session.close()


def test_cli_renders_notify_distinct_from_approval(capsys):
    from pyharness.cli.main import _trace

    _trace("notify", "blocked on 2FA — need you")
    out = capsys.readouterr().out
    assert "[agent note] blocked on 2FA — need you" in out
    assert "approval" not in out and "allow?" not in out
