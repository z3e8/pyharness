"""MCP transports: how JSON-RPC messages reach the server.

Two transports, one interface (`Transport`):

- `StdioTransport`  — a **local** server run as a subprocess; newline-delimited
  JSON-RPC 2.0 over its stdin/stdout (the MCP stdio transport).
- `HttpTransport`   — a **cloud / remote** server reached over the MCP
  *Streamable HTTP* transport: each request is an HTTP POST whose response is
  either a single JSON object or an SSE stream of JSON-RPC messages.

The client (`client.py`) is transport-agnostic: it builds JSON-RPC envelopes and
hands them to a transport, which is responsible only for delivery and for
correlating a request id with its response. A *response* is a message carrying
`result`/`error` and no `method` — a server-initiated request (`ping`,
`roots/list`) can reuse an id and must never be mistaken for a reply.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from typing import Protocol, runtime_checkable

# Protocol version we advertise at `initialize` (the current stable spec).
# Servers negotiate down if needed; the client records the negotiated version
# on the transport (`protocol_version`) after `initialize`.
PROTOCOL_VERSION = "2025-11-25"

# Lines of server stderr kept for diagnostics when a local server misbehaves.
_STDERR_TAIL = 40


class MCPError(RuntimeError):
    """An MCP transport/protocol failure or a server-returned JSON-RPC error."""


@runtime_checkable
class Transport(Protocol):
    # The negotiated protocol version; the client sets it after `initialize`.
    protocol_version: str

    def request(self, message: dict) -> dict:
        """Send a JSON-RPC request and return its response object."""

    def notify(self, message: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""

    def close(self) -> None: ...


def _is_response(message: dict) -> bool:
    return "method" not in message and ("result" in message or "error" in message)


class StdioTransport:
    """Local server as a subprocess; one JSON-RPC message per line.

    A daemon reader thread queues responses so `request()` can time out instead
    of blocking forever on a hung server, and answers server-initiated `ping`s.
    stderr is drained continuously (an undrained pipe deadlocks a chatty
    server) into a bounded tail that failure messages include."""

    def __init__(
        self,
        command: str,
        args: tuple[str, ...] = (),
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 30.0,
    ):
        self.protocol_version = PROTOCOL_VERSION
        self._timeout = timeout
        self._proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **(env or {})},
            cwd=cwd,
            text=True,
            bufsize=1,
        )
        self._responses: queue.Queue[dict | None] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL)
        self._write_lock = threading.Lock()
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def request(self, message: dict) -> dict:
        self._write(message)
        deadline = time.monotonic() + self._timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(
                    f"MCP server did not respond within {self._timeout}s{self._stderr_suffix()}"
                )
            try:
                reply = self._responses.get(timeout=remaining)
            except queue.Empty:
                continue  # loop re-checks the deadline and raises
            if reply is None:  # reader hit EOF — the server is gone
                raise MCPError(f"MCP server closed the connection{self._stderr_suffix()}")
            if reply.get("id") == message["id"]:
                return reply
            # else: a stale reply to an earlier timed-out request; drop it.

    def notify(self, message: dict) -> None:
        self._write(message)

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _read_loop(self) -> None:
        """Route stdout: responses to the queue, server `ping`s answered, other
        server-initiated traffic ignored. A sentinel marks EOF so a pending
        `request()` fails fast instead of waiting out its timeout."""
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue  # non-JSON noise on stdout
            if _is_response(message):
                self._responses.put(message)
            elif message.get("method") == "ping" and message.get("id") is not None:
                try:
                    self._write({"jsonrpc": "2.0", "id": message["id"], "result": {}})
                except MCPError:
                    break
        self._responses.put(None)

    def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr_tail.append(line.rstrip("\n"))

    def _stderr_suffix(self) -> str:
        # Error path only: if the server already exited, let the drain finish so
        # the message includes everything it said before dying.
        if self._proc.poll() is not None:
            self._stderr_thread.join(timeout=1)
        if not self._stderr_tail:
            return ""
        return "; server stderr: " + " | ".join(self._stderr_tail)

    def _write(self, message: dict) -> None:
        with self._write_lock:
            if self._proc.stdin is None or self._proc.poll() is not None:
                raise MCPError(f"MCP server process is not running{self._stderr_suffix()}")
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()


class HttpTransport:
    """Remote server over the MCP Streamable HTTP transport.

    Each call POSTs a JSON-RPC message to a single endpoint. The server may
    answer with `application/json` (one response) or `text/event-stream` (SSE);
    both are handled. A `Mcp-Session-Id` header issued at `initialize` is echoed
    on every subsequent request to keep the server-side session."""

    def __init__(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0):
        import httpx  # provided transitively via the anthropic dependency

        self.protocol_version = PROTOCOL_VERSION
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
                    if _is_response(event) and event.get("id") == message["id"]:
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
            "MCP-Protocol-Version": self.protocol_version,
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
