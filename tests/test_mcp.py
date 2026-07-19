import inspect
import sys
from pathlib import Path

import pytest

from pyharness import Registry
from pyharness.broker.remote.host import _seal_for_wire
from pyharness.broker.remote.protocol import RemoteToolSpec
from pyharness.tools.mcp import MCPClient, wrap_mcp_server

FAKE = Path(__file__).parent / "mcp_server_fake.py"
FAKE_TOOLS = {"echo", "add", "getenv", "read-status", "drop_table"}
FAKE_FUNCS = {"echo", "add", "getenv", "read_status", "drop_table"}


@pytest.fixture
def client():
    c = MCPClient.stdio(sys.executable, (str(FAKE),))
    yield c
    c.close()


def test_lists_and_calls_tools(client):
    names = {t["name"] for t in client.list_tools()}
    assert names == FAKE_TOOLS
    assert client.call_tool("echo", {"message": "hi"}) == "hi"
    assert client.call_tool("echo", {"message": "hi", "shout": True}) == "HI"
    assert client.call_tool("add", {"a": 2, "b": 3}) == "5"


def test_generated_signatures_reflect_schema():
    module = wrap_mcp_server("demo", sys.executable, (str(FAKE),))
    try:
        sig = inspect.signature(module.echo)
        assert list(sig.parameters) == ["message", "shout"]
        assert sig.parameters["message"].annotation is str
        assert sig.parameters["shout"].default is False
        assert "Echo a message back." in module.echo.__doc__
        # Generated functions call through and return the server's result.
        assert module.add(a=4, b=5) == "9"
    finally:
        module._mcp_client.close()


def test_dash_named_params_and_tool_names():
    """A '-' in a tool or param name coerces to a Python identifier, but the
    call must send the server's original property names."""
    module = wrap_mcp_server("demo", sys.executable, (str(FAKE),))
    try:
        assert list(inspect.signature(module.read_status).parameters) == ["dry_run"]
        assert module.read_status(dry_run=True) == '["dry-run"]'
        assert module.read_status() == "[]"  # unset optional stays omitted
        # The unset-marker default renders readably in describe_tool output,
        # not as <object object at 0x...>.
        sig = str(inspect.signature(module.read_status))
        assert "dry_run: bool = UNSET" in sig
    finally:
        module._mcp_client.close()


def test_module_carries_mcp_tool_metadata():
    module = wrap_mcp_server("demo", sys.executable, (str(FAKE),))
    try:
        meta = module._mcp_tools
        assert meta["read_status"]["name"] == "read-status"
        assert meta["read_status"]["annotations"] == {"readOnlyHint": True}
        assert meta["drop_table"]["annotations"] == {"destructiveHint": True}
        assert meta["echo"]["annotations"] == {}
    finally:
        module._mcp_client.close()


def test_colliding_coerced_names_are_disambiguated():
    from pyharness.tools.mcp.module import _unique

    assert _unique("run_it", {"run_it"}) == "run_it_2"
    assert _unique("run_it", {"run_it", "run_it_2"}) == "run_it_3"
    assert _unique("fresh", {"run_it"}) == "fresh"


def test_registry_discovery_and_use():
    registry = Registry()
    registry.add_mcp_server("demo", sys.executable, (str(FAKE),))
    try:
        listing = registry.search("demo")
        assert "# demo" in listing  # search returns headers, not signatures
        details = registry.describe("demo")
        assert "echo(message: str" in details
        assert "Echo a message back." in details
        module = registry.use("demo")
        assert module.echo(message="yo") == "yo"
    finally:
        registry.use("demo")._mcp_client.close()


def test_registry_search_wildcard_lists_tools():
    from types import ModuleType

    registry = Registry()
    registry.register(
        ModuleType("widget"), source="installed"
    )  # doc-less throwaway tool
    listing = registry.search("*")
    assert "# widget" in listing


def test_stdio_request_times_out_instead_of_hanging():
    from pyharness.tools.mcp.transport import MCPError, StdioTransport

    transport = StdioTransport(
        sys.executable, ("-c", "import time; time.sleep(30)"), timeout=0.5
    )
    try:
        with pytest.raises(MCPError, match="did not respond"):
            transport.request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
    finally:
        transport.close()


def test_stdio_failure_includes_stderr_tail():
    from pyharness.tools.mcp.transport import MCPError, StdioTransport

    code = "import sys; print('boom: bad credentials', file=sys.stderr)"
    transport = StdioTransport(sys.executable, ("-c", code), timeout=5)
    try:
        with pytest.raises(MCPError, match="boom: bad credentials"):
            transport.request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
    finally:
        transport.close()


def test_server_ping_request_is_not_mistaken_for_a_reply(client):
    """The fake emits a server-initiated ping whose id collides with the
    pending request; the client must skip it (and answer it) and still return
    the real response."""
    assert client.call_tool("echo", {"message": "__ping_first__"}) == "__ping_first__"


def test_remote_seal_yields_tool_spec():
    """Out-of-process, the wrapped module crosses the wire as a RemoteToolSpec
    (rebuilt child-side as a proxy that routes back through tools.invoke)."""
    module = wrap_mcp_server("demo", sys.executable, (str(FAKE),))
    try:
        spec = _seal_for_wire(module)
        assert isinstance(spec, RemoteToolSpec)
        assert spec.name == "demo"
        assert set(spec.functions) == FAKE_FUNCS
    finally:
        module._mcp_client.close()
