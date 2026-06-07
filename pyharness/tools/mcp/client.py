"""The transport-agnostic MCP client and connection helpers."""

from __future__ import annotations

from types import ModuleType
from typing import Any

from .module import build_module
from .transport import (
    PROTOCOL_VERSION,
    HttpTransport,
    MCPError,
    StdioTransport,
    Transport,
)


class MCPClient:
    """Speaks MCP (initialize, tools/list, tools/call) over any `Transport`.

    Lives in the parent process; the agent never holds it. Build one with
    `MCPClient.stdio(...)` for a local server or `MCPClient.http(...)` for a
    remote one, or use `connect(...)`."""

    def __init__(self, transport: Transport):
        self._transport = transport
        self._next_id = 0
        self._tools: list[dict] | None = None
        self._initialize()

    @classmethod
    def stdio(cls, command: str, args: tuple[str, ...] = (), *, env=None, cwd=None) -> "MCPClient":
        return cls(StdioTransport(command, args, env=env, cwd=cwd))

    @classmethod
    def http(cls, url: str, *, headers=None, timeout: float = 30.0) -> "MCPClient":
        return cls(HttpTransport(url, headers=headers, timeout=timeout))

    def list_tools(self) -> list[dict]:
        """The server's tool descriptors (name, description, inputSchema), cached."""
        if self._tools is None:
            self._tools = self._request("tools/list").get("tools", [])
        return self._tools

    def call_tool(self, name: str, arguments: dict) -> Any:
        """Invoke one tool; return its structured content or joined text. Raises
        `MCPError` on a tool-level error."""
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise MCPError(_text_content(result))
        if "structuredContent" in result:
            return result["structuredContent"]
        return _text_content(result)

    def close(self) -> None:
        self._transport.close()

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pyharness", "version": "0.1"},
            },
        )
        self._transport.notify(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        reply = self._transport.request(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        )
        if "error" in reply:
            raise MCPError(f"{method}: {reply['error']}")
        return reply.get("result", {})


def connect(
    *,
    command: str | None = None,
    args: tuple[str, ...] = (),
    url: str | None = None,
    env=None,
    headers=None,
    cwd=None,
    timeout: float = 30.0,
) -> MCPClient:
    """Open a client to a local (`command`) or remote (`url`) MCP server."""
    if url and command:
        raise ValueError("specify either url (remote) or command (local), not both")
    if url:
        return MCPClient.http(url, headers=headers, timeout=timeout)
    if command:
        return MCPClient.stdio(command, tuple(args), env=env, cwd=cwd)
    raise ValueError("specify command (local stdio) or url (remote http)")


def wrap_mcp_server(
    name: str,
    command: str | None = None,
    args: tuple[str, ...] = (),
    *,
    url: str | None = None,
    env=None,
    headers=None,
    cwd=None,
    summary: str | None = None,
    timeout: float = 30.0,
) -> ModuleType:
    """Connect to an MCP server and return it as a tool module."""
    client = connect(
        command=command, args=tuple(args), url=url, env=env, headers=headers, cwd=cwd, timeout=timeout
    )
    return build_module(client, name, summary=summary)


def _text_content(result: dict) -> str:
    parts = [
        block.get("text", "")
        for block in result.get("content", [])
        if block.get("type") == "text"
    ]
    return "\n".join(parts)
