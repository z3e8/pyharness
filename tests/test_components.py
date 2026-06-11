import pytest

from pyharness import Budget, Decision, Policy, Registry, Vault, Workspace
from pyharness.audit import AuditLog
from pyharness.broker import Broker, PermissionDenied
from pyharness.broker.capabilities import FilesCapability
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
    assert "calc" in reg.search()
    assert "evaluate" in reg.search("calc")
    assert reg.use("calc").evaluate("2 + 3 * 4") == 14


def test_registry_discovers_multiple_tools():
    reg = Registry()
    listing = reg.search()
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


def test_web_fetch_injects_auth_parent_side(tmp_path, monkeypatch):
    import httpx

    from pyharness.broker.capabilities import WebCapability

    captured = {}

    class FakeResp:
        text = "ok"

    def fake_get(url, **kwargs):
        captured.update(url=url, headers=kwargs.get("headers"), params=kwargs.get("params"))
        return FakeResp()

    monkeypatch.setattr(httpx, "get", fake_get)
    web = WebCapability(llm=None, vault=Vault({"k": "S3CRET"}))

    web.web_fetch("http://x", auth="k")
    assert captured["headers"]["Authorization"] == "Bearer S3CRET"

    web.web_fetch("http://x", auth="k", auth_style="header", auth_name="X-API-Key")
    assert captured["headers"]["X-API-Key"] == "S3CRET"

    web.web_fetch("http://x", auth="k", auth_style="query", auth_name="api_key")
    assert captured["params"] == {"api_key": "S3CRET"}

    web.web_fetch("http://x", auth="k", auth_style="basic", user="alice")
    import base64

    assert captured["headers"]["Authorization"] == "Basic " + base64.b64encode(b"alice:S3CRET").decode()
