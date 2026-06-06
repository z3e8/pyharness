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


def test_vault_never_via_namespace(tmp_path):
    # The vault is reachable by trusted code but is not a kernel function.
    broker = _broker(tmp_path)
    broker.register(FilesCapability(Workspace(tmp_path)))
    assert "get" not in broker.namespace()
    assert Vault({"token": "secret"}).get("token") == "secret"
