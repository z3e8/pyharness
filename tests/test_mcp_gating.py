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
from pyharness.broker.capabilities.tools import unvetted_mcp_call
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
    policy = Policy(approve_if=[unvetted_mcp_call(lambda: registry)])
    broker = Broker(policy, AuditLog(tmp_path / "audit.jsonl"), Budget(), approver=approver)
    cap = ToolsCapability(registry, broker=broker)
    broker.register(cap)
    return registry, broker, cap


def _audited_actions(tmp_path):
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    return [json.loads(line).get("action") for line in lines]


def test_use_tool_routes_mcp_calls_through_the_broker(tmp_path):
    registry, broker, cap = _setup(tmp_path)
    try:
        demo = cap.use_tool("demo")
        # Read-only per the server's annotations: flows without approval.
        assert demo.read_status() == "[]"
        assert "tools.invoke" in _audited_actions(tmp_path)
        # The gated proxy keeps the generated signature and docstring.
        assert list(inspect.signature(demo.echo).parameters) == ["message", "shout"]
        assert "Echo a message back." in demo.echo.__doc__
    finally:
        registry.close()


def test_mutating_mcp_call_requires_approval(tmp_path):
    approver = _Approver()
    registry, broker, cap = _setup(tmp_path, approver=approver)
    try:
        demo = cap.use_tool("demo")
        assert demo.echo(message="hi") == "hi"  # un-annotated -> prompted, allowed
        (request,) = approver.requests
        assert request.category is ActionCategory.OUTWARD
        assert "demo.echo(" in request.summary
        assert request.scope is not None
        assert (request.scope.action_class, request.scope.target) == ("mcp", "demo")
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


def test_grant_covers_the_server_for_the_session(tmp_path):
    approver = _Approver(outcome=ApprovalOutcome.GRANT)
    registry, broker, cap = _setup(tmp_path, approver=approver)
    try:
        demo = cap.use_tool("demo")
        assert demo.echo(message="one") == "one"
        assert demo.echo(message="two") == "two"  # covered by the minted grant
        assert demo.add(a=1, b=2) == "3"  # same server, same grant
        assert len(approver.requests) == 1
    finally:
        registry.close()


def test_destructive_mcp_call_is_irreversible_and_never_grantable(tmp_path):
    approver = _Approver(outcome=ApprovalOutcome.GRANT)
    registry, broker, cap = _setup(tmp_path, approver=approver)
    try:
        demo = cap.use_tool("demo")
        assert demo.drop_table(table="users") == "dropped users"
        assert demo.drop_table(table="orders") == "dropped orders"
        # Both calls prompted (GRANT could not mint for an IRREVERSIBLE call).
        assert len(approver.requests) == 2
        assert all(r.category is ActionCategory.IRREVERSIBLE for r in approver.requests)
        assert all(r.scope is None for r in approver.requests)
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


def test_session_wires_mcp_gating_in_process(tmp_path):
    config = {"mcpServers": {"demo": {"command": sys.executable, "args": [str(FAKE)]}}}
    session = Session(tmp_path, mcp_config=config)  # in-process, no approver
    try:
        use_tool = session.broker.namespace()["use_tool"]
        demo = use_tool("demo")
        assert demo.read_status() == "[]"  # read-only flows
        with pytest.raises(PermissionDenied):
            demo.echo(message="hi")  # mutating is gated, in-process too
    finally:
        session.close()
