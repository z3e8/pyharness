"""MCP transports: how JSON-RPC messages reach the server.

Two transports, one interface (`Transport`):

- `StdioTransport`  — a **local** server run as a subprocess; newline-delimited
  JSON-RPC 2.0 over its stdin/stdout (the MCP stdio transport).
- `HttpTransport`   — a **cloud / remote** server reached over the MCP
  *Streamable HTTP* transport: each request is an HTTP POST whose response is
  either a single JSON object or an SSE stream of JSON-RPC messages.

The client (`client.py`) is transport-agnostic: it builds JSON-RPC envelopes and
hands them to a transport, which is responsible only for delivery and for
correlating a request id with its response.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Protocol, runtime_checkable

# Protocol version we advertise at `initialize`. Servers negotiate down if needed.
PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    """An MCP transport/protocol failure or a server-returned JSON-RPC error."""


@runtime_checkable
class Transport(Protocol):
    def request(self, message: dict) -> dict:
        """Send a JSON-RPC request and return its response object."""

    def notify(self, message: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""

    def close(self) -> None: ...


class StdioTransport:
    """Local server as a subprocess; one JSON-RPC message per line."""

    def __init__(
        self,
        command: str,
        args: tuple[str, ...] = (),
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ):
        self._proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **(env or {})},
            cwd=cwd,
            text=True,
            bufsize=1,
        )

    def request(self, message: dict) -> dict:
        self._write(message)
        expected = message["id"]
        while True:  # skip any server-initiated notifications/requests
            reply = self._read()
            if reply.get("id") == expected:
                return reply

    def notify(self, message: dict) -> None:
        self._write(message)

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _write(self, message: dict) -> None:
        if self._proc.stdin is None or self._proc.poll() is not None:
            raise MCPError("MCP server process is not running")
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def _read(self) -> dict:
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            raise MCPError("MCP server closed the connection")
        return json.loads(line)


class HttpTransport:
    """Remote server over the MCP Streamable HTTP transport.

    Each call POSTs a JSON-RPC message to a single endpoint. The server may
    answer with `application/json` (one response) or `text/event-stream` (SSE);
    both are handled. A `Mcp-Session-Id` header issued at `initialize` is echoed
    on every subsequent request to keep the server-side session."""

    def __init__(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0):
        import httpx  # provided transitively via the anthropic dependency

        self._url = url
        self._client = httpx.Client(timeout=timeout, headers=headers or {})
        self._session_id: str | None = None

    def request(self, message: dict) -> dict:
        with self._client.stream("POST", self._url, json=message, headers=self._headers()) as resp:
            resp.raise_for_status()
            self._capture_session(resp)
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                for event in _iter_sse(resp):
                    if event.get("id") == message["id"]:
                        return event
                raise MCPError("no matching JSON-RPC response in SSE stream")
            return json.loads(resp.read())

    def notify(self, message: dict) -> None:
        with self._client.stream("POST", self._url, json=message, headers=self._headers()) as resp:
            resp.raise_for_status()
            self._capture_session(resp)
            resp.read()  # drain; servers reply 202 Accepted with no body

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _capture_session(self, resp) -> None:
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid


def _iter_sse(resp):
    """Yield the JSON payload of each SSE `data:` event from a streamed response."""
    data: list[str] = []
    for line in resp.iter_lines():
        if line == "":  # event boundary
            if data:
                yield json.loads("\n".join(data))
                data = []
        elif line.startswith("data:"):
            data.append(line[len("data:"):].lstrip())
        # comment lines (":...") and other fields (event:, id:) are ignored
    if data:
        yield json.loads("\n".join(data))
