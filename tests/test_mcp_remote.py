"""Remote (Streamable HTTP) transport + config-driven mounting."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from pyharness import Registry, Vault
from pyharness.tools.mcp import MCPClient, mount_config

FAKE = Path(__file__).parent / "mcp_server_fake.py"

# Reuse the stdio fake's tool set for the HTTP server's responses.
_TOOLS = [
    {
        "name": "ping",
        "description": "Return pong.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


class _Handler(BaseHTTPRequestHandler):
    """A minimal MCP Streamable HTTP server: replies to each POSTed JSON-RPC
    request with a single application/json response."""

    def log_message(self, *args):
        pass  # quiet

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.seen_versions.append(self.headers.get("MCP-Protocol-Version"))
        msg = json.loads(body)
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:  # notification (e.g. notifications/initialized)
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": _TOOLS}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "pong"}]}
        else:
            result = {}
        payload = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "test-session")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def http_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.seen_versions = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture
def http_url(http_server):
    host, port = http_server.server_address
    return f"http://{host}:{port}/mcp"


def test_http_transport_round_trip(http_url, http_server):
    from pyharness.tools.mcp.transport import PROTOCOL_VERSION

    client = MCPClient.http(http_url)
    try:
        assert {t["name"] for t in client.list_tools()} == {"ping"}
        assert client.call_tool("ping", {}) == "pong"
    finally:
        client.close()
    # We advertise the current version, then echo whatever the server
    # negotiated down to on every subsequent request.
    assert http_server.seen_versions[0] == PROTOCOL_VERSION
    assert set(http_server.seen_versions[1:]) == {"2024-11-05"}


def test_registry_add_remote_server(http_url):
    registry = Registry()
    registry.add_mcp_server("remote", url=http_url)
    try:
        assert "# remote" in registry.search("remote")  # header only
        assert "ping()" in registry.describe("remote")  # signatures via describe
        assert registry.use("remote").ping() == "pong"
    finally:
        registry.close()


def test_config_mounts_local_and_resolves_secrets():
    registry = Registry()
    vault = Vault({"api_key": "sk-from-vault"})
    config = {
        "mcpServers": {
            "demo": {
                "command": sys.executable,
                "args": [str(FAKE)],
                "env": {"DEMO_KEY": "secret:api_key"},
            }
        }
    }
    mount_config(registry, config, vault=vault)
    try:
        # The secret was resolved from the vault and injected into the server's
        # process env — the tool reads it back, proving parent-side injection.
        assert registry.use("demo").getenv(name="DEMO_KEY") == "sk-from-vault"
    finally:
        registry.close()


def test_config_forwards_discovery_metadata():
    """keywords/category/featured declared in the config reach the registry, so
    a lazily-mounted (never-connected) server is findable by what it does."""
    registry = Registry()
    config = {
        "mcpServers": {
            "tracker": {
                "command": "/no/such/binary-xyz",
                "summary": "Issue tracker.",
                "keywords": ["issues", "bugs"],
                "category": "vcs",
                "featured": True,
            }
        }
    }
    mount_config(registry, config)
    info = registry._tools["tracker"]
    assert info.kind == "mcp"
    assert info.keywords == ("issues", "bugs")
    assert info.category == "vcs"
    assert info.featured
    # A keyword query ranks the server without connecting it.
    assert "# tracker" in registry.search("bugs")
    assert info.module is None


def test_config_secret_without_vault_errors():
    registry = Registry()
    config = {"mcpServers": {"demo": {"command": "x", "env": {"K": "secret:missing"}}}}
    with pytest.raises(ValueError, match="secret"):
        mount_config(registry, config, vault=None)


def test_lazy_mount_does_not_connect_and_tolerates_down_servers():
    """A down server (bad command) must not break mounting; it is registered but
    only fails — gracefully — when actually reached."""
    registry = Registry()
    config = {
        "mcpServers": {
            "good": {"command": sys.executable, "args": [str(FAKE)]},
            "down": {"command": "/no/such/binary-xyz"},
        }
    }
    # Mounting connects to neither server, so the bad one does not raise here.
    assert mount_config(registry, config) == ["good", "down"]
    try:
        # Search connects to neither — both appear as headers marked "not loaded".
        listing = registry.search("", include_all=True)
        assert "# good" in listing and "# down" in listing
        assert "not loaded" in listing
        # describe() is what connects: the good server lists its tools; the down
        # one is reported as unavailable instead of raising.
        assert "echo(message: str" in registry.describe("good")
        assert "unavailable" in registry.describe("down")
        # The good server works on first use; the down one raises a clear error.
        assert registry.use("good").add(a=1, b=2) == "3"
        with pytest.raises(RuntimeError, match="unavailable"):
            registry.use("down")
    finally:
        registry.close()


def test_lazy_eager_opt_out_fails_fast():
    registry = Registry()
    config = {"mcpServers": {"down": {"command": "/no/such/binary-xyz"}}}
    with pytest.raises(Exception):
        mount_config(registry, config, lazy=False)
