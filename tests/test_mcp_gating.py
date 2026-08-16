"""Broker gating of MCP tool calls.

Both kernel modes funnel tool-module calls through `tools.invoke` (the child
via RemoteToolSpec proxies, the in-process kernel via `use_tool`'s gated
proxies), so these tests exercise that shared path: policy from annotations,
approval, grants, audit."""

import inspect
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from pyharness import Budget, Policy, Registry, Session
from pyharness.audit import AuditLog
from pyharness.broker import Broker
from pyharness.broker.capabilities import ToolsCapability
from pyharness.broker.capabilities.tools import mcp_tool_call
from pyharness.broker.dispatch import ApprovalOutcome, PermissionDenied
from pyharness.security.policy import ActionCategory

FAKE = Path(__file__).parent / "mcp_server_fake.py"


class _Approver:
    def __init__(self, outcome=ApprovalOutcome.ONCE):
        self.outcome = outcome
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.outcome


def _setup(tmp_path, approver=None):
    registry = Registry()
    registry.add_mcp_server("demo", sys.executable, (str(FAKE),))
    policy = Policy(approve_if=[mcp_tool_call(lambda: registry)])
    broker = Broker(
        policy, AuditLog(tmp_path / "audit.jsonl"), Budget(), approver=approver
    )
    cap = ToolsCapability(registry, broker=broker)
    broker.register(cap)
    return registry, broker, cap


def _audited_actions(tmp_path):
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    return [json.loads(line).get("action") for line in lines]


def test_use_tool_routes_mcp_calls_through_the_broker(tmp_path):
    registry, broker, cap = _setup(tmp_path, approver=_Approver())
    try:
        demo = cap.use_tool("demo")
        # A tool the server declares read-only is gated like any other: the
        # declaration is the server's own claim, so it buys no silence.
        assert demo.read_status() == "[]"
        assert "tools.invoke" in _audited_actions(tmp_path)
        # The gated proxy keeps the generated signature and docstring.
        assert list(inspect.signature(demo.echo).parameters) == ["message", "shout"]
        assert "Echo a message back." in demo.echo.__doc__
    finally:
        registry.close()


def test_read_only_annotation_does_not_suppress_approval(tmp_path):
    registry, broker, cap = _setup(tmp_path)  # no approver
    try:
        demo = cap.use_tool("demo")
        with pytest.raises(PermissionDenied):
            demo.read_status()
    finally:
        registry.close()


def test_mcp_call_requires_approval_scoped_to_the_one_tool(tmp_path):
    approver = _Approver()
    registry, broker, cap = _setup(tmp_path, approver=approver)
    try:
        demo = cap.use_tool("demo")
        assert demo.echo(message="hi") == "hi"
        (request,) = approver.requests
        assert request.category is ActionCategory.OUTWARD
        assert "demo.echo(" in request.summary
        assert request.scope is not None
        # The grant key names the tool, not just the server that carries it.
        assert (request.scope.action_class, request.scope.target) == (
            "mcp",
            "demo.echo",
        )
        assert request.scope.pin  # pinned to the descriptor the human was shown
    finally:
        registry.close()


def test_mutating_mcp_call_denied_without_approver(tmp_path):
    registry, broker, cap = _setup(tmp_path)  # no approver
    try:
        demo = cap.use_tool("demo")
        with pytest.raises(PermissionDenied):
            demo.echo(message="hi")
    finally:
        registry.close()


def test_grant_covers_one_tool_not_the_whole_server(tmp_path):
    approver = _Approver(outcome=ApprovalOutcome.GRANT)
    registry, broker, cap = _setup(tmp_path, approver=approver)
    try:
        demo = cap.use_tool("demo")
        assert demo.echo(message="one") == "one"
        assert demo.echo(message="two") == "two"  # covered by the minted grant
        assert len(approver.requests) == 1
        assert demo.add(a=1, b=2) == "3"  # a different tool: a different decision
        assert len(approver.requests) == 2
        assert approver.requests[-1].scope.target == "demo.add"
    finally:
        registry.close()


def test_grant_is_pinned_to_the_annotations_the_human_saw(tmp_path):
    approver = _Approver(outcome=ApprovalOutcome.GRANT)
    registry, broker, cap = _setup(tmp_path, approver=approver)
    try:
        demo = cap.use_tool("demo")
        assert demo.echo(message="one") == "one"
        assert demo.echo(message="two") == "two"  # same descriptor, grant holds
        assert len(approver.requests) == 1
        # The server re-declares the tool under the standing grant. The grant was
        # for what the human read, so the changed tool has to be asked again.
        registry.info("demo").module._mcp_tools["echo"]["annotations"] = {
            "destructiveHint": True
        }
        assert demo.echo(message="three") == "three"
        assert len(approver.requests) == 2
        assert approver.requests[0].scope.pin != approver.requests[1].scope.pin
    finally:
        registry.close()


def test_grant_is_pinned_to_the_mcp_name_behind_the_python_function(tmp_path):
    approver = _Approver(outcome=ApprovalOutcome.GRANT)
    registry, broker, cap = _setup(tmp_path, approver=approver)
    try:
        demo = cap.use_tool("demo")
        assert demo.echo(message="one") == "one"
        # Swapping which server-side tool `echo` forwards to is the same trick as
        # changing its hints, and the pin covers it too.
        registry.info("demo").module._mcp_tools["echo"]["name"] = "drop_table"
        demo.echo(message="two")
        assert len(approver.requests) == 2
    finally:
        registry.close()


def test_destructive_hint_is_shown_but_decides_nothing(tmp_path):
    approver = _Approver(outcome=ApprovalOutcome.GRANT)
    registry, broker, cap = _setup(tmp_path, approver=approver)
    try:
        demo = cap.use_tool("demo")
        assert demo.drop_table(table="users") == "dropped users"
        (request,) = approver.requests
        # The server's claim reaches the human as a claim, and nothing else: the
        # category stays what the harness itself can vouch for, and the call is
        # grantable per tool like any other.
        assert request.category is ActionCategory.OUTWARD
        assert "declares destructive" in request.summary
        assert request.scope.target == "demo.drop_table"
        # ... and the grant it minted still stops at that one tool.
        assert demo.drop_table(table="orders") == "dropped orders"
        assert len(approver.requests) == 1
        demo.echo(message="hi")
        assert len(approver.requests) == 2
    finally:
        registry.close()


def test_unresolved_mcp_target_fails_closed(tmp_path):
    registry, broker, cap = _setup(tmp_path)
    registry.register_lazy("ghost", loader=lambda: ModuleType("ghost"), kind="mcp")
    try:
        # Policy can't inspect an unconnected server's annotations, so the call
        # requires approval (and there is no approver) — and crucially, deciding
        # that did not connect the server.
        with pytest.raises(PermissionDenied):
            broker.call("tools", "invoke", "ghost", "anything", (), {})
        assert registry.info("ghost").module is None
    finally:
        registry.close()


def test_non_mcp_modules_stay_ungated_but_audited(tmp_path):
    registry, broker, cap = _setup(tmp_path)
    mod = ModuleType("widget")

    def hello():
        return "hi"

    hello.__module__ = "widget"
    mod.hello = hello
    registry.register(mod, source="installed")
    try:
        widget = cap.use_tool("widget")
        assert widget.hello() == "hi"  # no approver needed
        assert "tools.invoke" in _audited_actions(tmp_path)
    finally:
        registry.close()


def test_broker_gated_core_modules_are_not_wrapped_twice(tmp_path):
    registry, broker, cap = _setup(tmp_path)
    try:
        core = broker.as_tool_module("tools", summary="the tools ops")
        registry.register(core, source="core", name="toolsmod")
        assert cap.use_tool("toolsmod") is core  # passed through untouched
    finally:
        registry.close()


def _mount_setup(tmp_path, approver=None, config_path=None, vault=None):
    registry = Registry()
    policy = Policy(
        require_approval={"tools.add_mcp_server"},
        approve_if=[mcp_tool_call(lambda: registry)],
    )
    broker = Broker(
        policy, AuditLog(tmp_path / "audit.jsonl"), Budget(), approver=approver
    )
    cap = ToolsCapability(
        registry, broker=broker, vault=vault, mcp_config_path=config_path
    )
    broker.register(cap)
    return registry, broker, cap


def test_add_mcp_server_mounts_lazily_after_approval(tmp_path):
    approver = _Approver()
    registry, broker, cap = _mount_setup(tmp_path, approver=approver)
    try:
        result = broker.call(
            "tools", "add_mcp_server", "demo", sys.executable, (str(FAKE),)
        )
        assert "mounted MCP server 'demo'" in result
        (request,) = approver.requests
        assert "mount MCP server 'demo'" in request.summary
        assert request.scope is None  # never grantable
        assert registry.info("demo").kind == "mcp"
        assert registry.info("demo").module is None  # lazy: not yet connected
        assert cap.use_tool("demo").read_status() == "[]"
    finally:
        registry.close()


def test_add_mcp_server_denied_without_approver(tmp_path):
    registry, broker, cap = _mount_setup(tmp_path)
    try:
        with pytest.raises(PermissionDenied):
            broker.call("tools", "add_mcp_server", "demo", sys.executable, (str(FAKE),))
        assert registry.info("demo") is None
    finally:
        registry.close()


def test_add_mcp_server_refuses_existing_names(tmp_path):
    registry, broker, cap = _setup(tmp_path)  # "demo" already registered
    try:
        with pytest.raises(ValueError, match="already exists"):
            cap.add_mcp_server("demo", command="x")
        # Shadowing a core tool name is exactly the spoofing this prevents.
        registry.register(ModuleType("http"), source="core", name="http")
        with pytest.raises(ValueError, match="already exists"):
            cap.add_mcp_server("http", url="https://evil.example/mcp")
    finally:
        registry.close()


def test_add_mcp_server_save_writes_config_with_secret_refs(tmp_path):
    from pyharness import Vault

    config_path = tmp_path / "mcp.json"
    vault = Vault({"tok": "cleartext-token"})
    registry, broker, cap = _mount_setup(
        tmp_path, approver=_Approver(), config_path=config_path, vault=vault
    )
    try:
        cap.add_mcp_server(
            "demo",
            command=sys.executable,
            args=(str(FAKE),),
            env={"DEMO_KEY": "secret:tok"},
            save=True,
        )
        saved = json.loads(config_path.read_text())
        assert saved["mcpServers"]["demo"]["env"] == {"DEMO_KEY": "secret:tok"}
        assert "cleartext-token" not in config_path.read_text()
        # The mounted server still resolves the secret through the vault.
        assert cap.use_tool("demo").getenv(name="DEMO_KEY") == "cleartext-token"
    finally:
        registry.close()


def test_add_mcp_server_save_refuses_cleartext_credentials(tmp_path):
    config_path = tmp_path / "mcp.json"
    registry, broker, cap = _mount_setup(tmp_path, config_path=config_path)
    try:
        with pytest.raises(ValueError, match="secret:NAME"):
            cap.add_mcp_server(
                "demo",
                url="https://x/mcp",
                headers={"Authorization": "Bearer abc"},
                save=True,
            )
        assert not config_path.exists()
        assert registry.info("demo") is None  # nothing mounted either
    finally:
        registry.close()


def test_add_mcp_server_save_requires_a_config_path(tmp_path):
    registry, broker, cap = _mount_setup(tmp_path)  # no config path
    try:
        with pytest.raises(ValueError, match="no MCP config path"):
            cap.add_mcp_server("demo", command="x", save=True)
    finally:
        registry.close()


def test_session_wires_mcp_gating_in_process(tmp_path):
    config = {"mcpServers": {"demo": {"command": sys.executable, "args": [str(FAKE)]}}}
    session = Session(
        tmp_path, mcp_config=config, unsafe_in_process=True
    )  # no approver
    try:
        use_tool = session.broker.namespace()["use_tool"]
        demo = use_tool("demo")
        # A server declared in the session's MCP config mounts without a prompt —
        # an operator put it there. That provenance covers *connecting*, and
        # stops there: every call still needs its own human, in-process too.
        with pytest.raises(PermissionDenied):
            demo.read_status()  # even the one the server calls read-only
        with pytest.raises(PermissionDenied):
            demo.echo(message="hi")
    finally:
        session.close()
