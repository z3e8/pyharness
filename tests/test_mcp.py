import inspect
import sys
from pathlib import Path

import pytest

from pyharness import Registry
from pyharness.broker.remote.host import _seal_for_wire
from pyharness.broker.remote.protocol import RemoteToolSpec
from pyharness.tools.mcp import MCPClient, wrap_mcp_server

FAKE = Path(__file__).parent / "mcp_server_fake.py"


@pytest.fixture
def client():
    c = MCPClient.stdio(sys.executable, (str(FAKE),))
    yield c
    c.close()


def test_lists_and_calls_tools(client):
    names = {t["name"] for t in client.list_tools()}
    assert names == {"echo", "add", "getenv"}
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


def test_registry_discovery_and_use():
    registry = Registry()
    registry.add_mcp_server("demo", sys.executable, (str(FAKE),))
    try:
        interface = registry.search("demo")
        assert "echo(message: str" in interface
        assert "Echo a message back." in interface
        module = registry.use("demo")
        assert module.echo(message="yo") == "yo"
    finally:
        registry.use("demo")._mcp_client.close()


def test_registry_search_wildcard_lists_tools():
    registry = Registry()
    listing = registry.search("*")
    assert "# calc" in listing


def test_remote_seal_yields_tool_spec():
    """Out-of-process, the wrapped module crosses the wire as a RemoteToolSpec
    (rebuilt child-side as a proxy that routes back through tools.invoke)."""
    module = wrap_mcp_server("demo", sys.executable, (str(FAKE),))
    try:
        spec = _seal_for_wire(module)
        assert isinstance(spec, RemoteToolSpec)
        assert spec.name == "demo"
        assert set(spec.functions) == {"echo", "add", "getenv"}
    finally:
        module._mcp_client.close()
