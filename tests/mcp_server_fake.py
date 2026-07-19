"""A minimal MCP stdio server for tests.

Speaks just enough of the protocol — initialize, tools/list, tools/call — over
newline-delimited JSON-RPC 2.0 to exercise the real `MCPClient` round trip.
Exposes two tools: `echo` (required arg) and `add` (typed numeric args).
"""

import json
import os
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo a message back.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "text to echo"},
                "shout": {
                    "type": "boolean",
                    "description": "uppercase it",
                    "default": False,
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "getenv",
        "description": "Return one of the server's environment variables.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "read-status",
        "description": "A read-only tool with a dash-named parameter.",
        "inputSchema": {
            "type": "object",
            "properties": {"dry-run": {"type": "boolean"}},
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "drop_table",
        "description": "A destructive tool.",
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
        "annotations": {"destructiveHint": True},
    },
]


def _call(name, args):
    if name == "echo":
        text = args["message"]
        return text.upper() if args.get("shout") else text
    if name == "add":
        return str(args["a"] + args["b"])
    if name == "getenv":
        return os.environ.get(args["name"], "")
    if name == "read-status":
        # Echo back the argument keys so tests can assert the client sent the
        # server's own property names (e.g. "dry-run", not "dry_run").
        return json.dumps(sorted(args))
    if name == "drop_table":
        return f"dropped {args['table']}"
    raise ValueError(f"unknown tool {name}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:
            continue  # a notification (e.g. notifications/initialized)
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg["params"]
            if params.get("arguments", {}).get("message") == "__ping_first__":
                # Emit a server-initiated ping whose id collides with the
                # pending request, so tests can prove the client matches
                # responses (result/error, no method) rather than bare ids.
                ping = {"jsonrpc": "2.0", "id": msg_id, "method": "ping", "params": {}}
                sys.stdout.write(json.dumps(ping) + "\n")
                sys.stdout.flush()
            text = _call(params["name"], params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": text}]}
        elif method is None:
            continue  # a response to a server-initiated request (e.g. our ping)
        else:
            result = {}
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()
