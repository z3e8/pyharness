import pytest

from pyharness import Budget, Decision, Policy, Registry, Vault, Workspace
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


def test_policy_approval(tmp_path):
    seen = {}

    def approver(action, args, kwargs):
        seen["action"] = action
        return True

    broker = _broker(tmp_path, policy=Policy(require_approval={"files.write"}), approver=approver)
    broker.register(FilesCapability(Workspace(tmp_path)))
    broker.namespace()["write"]("x.txt", "y")
    assert seen["action"] == "files.write"


def test_budget_records_and_limits():
    b = Budget(limit_usd=0.01)
    b.check()  # under limit, fine
    b.record("claude-opus-4-8", 0.02)
    import pytest

    with pytest.raises(BudgetExceeded):
        b.check()


def test_registry_discovers_builtin_calc():
    reg = Registry()
    assert "calc" in reg.search()  # featured, so it shows for an empty query
    assert "evaluate" in reg.describe("calc")  # signatures come from describe
    assert reg.use("calc").evaluate("2 + 3 * 4") == 14


def test_registry_discovers_multiple_tools():
    reg = Registry()
    listing = reg.search("", include_all=True)  # empty query alone shows only featured
    for name in ("calc", "clock", "text"):
        assert name in listing
    assert reg.use("text").counts("a b c\nd") == {"chars": 7, "words": 4, "lines": 2}


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


class _FakeResp:
    def __init__(self, status=200, text="ok", url="http://x"):
        import datetime

        self.status_code = status
        self.text = text
        self.url = url
        self.headers = {"content-type": "text/plain"}
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

    def approver(action, args, kwargs):
        prompted.append(args[1])
        return False

    broker = _broker(tmp_path, policy=Policy(approve_if=[_is_mutating_http]), approver=approver)
    broker.register(HttpSessionCapability(Workspace(tmp_path)))
    ns = broker.namespace()

    ns["request"](None, "GET", "http://x")  # read: no approval prompt
    assert prompted == []

    with pytest.raises(PermissionDenied):
        ns["request"](None, "POST", "http://x")  # write: gated, and denied here
    assert prompted == ["POST"]


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
