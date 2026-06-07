"""Generate a tool module (typed Python functions) from an MCP server.

Each MCP tool descriptor becomes a Python function whose signature, annotations,
and docstring are derived from its JSON Schema, so `Registry.search()` shows a
real interface and the agent writes ordinary Python. Calls forward to the
server via the client. The generated module enters the normal registry; MCP is
never an interface the agent sees (design §6).
"""

from __future__ import annotations

import inspect
from types import ModuleType

# JSON Schema primitive -> Python annotation, for readable generated signatures.
_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}

# Marks an optional parameter the caller did not supply, so we omit it from the
# call arguments rather than sending a null the server didn't ask for.
_UNSET = object()


def build_module(client, name: str, *, summary: str | None = None) -> ModuleType:
    """Generate a module exposing one function per tool on `client`."""
    tools = client.list_tools()
    module = ModuleType(name)
    module.__doc__ = summary or f"MCP server {name!r} ({len(tools)} tools)."
    module._mcp_client = client  # keep the client alive with the module
    for tool in tools:
        func = _make_function(client, name, tool)
        setattr(module, func.__name__, func)
    return module


def _make_function(client, module_name: str, tool: dict):
    tool_name = tool["name"]
    schema = tool.get("inputSchema") or {}
    signature = _build_signature(schema)

    def call(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = {k: v for k, v in bound.arguments.items() if v is not _UNSET}
        return client.call_tool(tool_name, arguments)

    call.__name__ = _identifier(tool_name)
    call.__qualname__ = call.__name__
    call.__module__ = module_name  # so Registry._public_functions discovers it
    call.__signature__ = signature
    call.__doc__ = _docstring(tool, schema)
    return call


def _build_signature(schema: dict) -> inspect.Signature:
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    params, optionals = [], []
    for prop, spec in properties.items():
        annotation = _JSON_TYPES.get(spec.get("type"), inspect.Parameter.empty)
        if prop in required:
            params.append(
                inspect.Parameter(
                    _identifier(prop),
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=annotation,
                )
            )
        else:
            optionals.append(
                inspect.Parameter(
                    _identifier(prop),
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=spec.get("default", _UNSET),
                    annotation=annotation,
                )
            )
    return inspect.Signature(params + optionals)


def _docstring(tool: dict, schema: dict) -> str:
    lines = [(tool.get("description") or tool["name"]).strip()]
    properties = schema.get("properties", {})
    if properties:
        lines.append("")
        for prop, spec in properties.items():
            desc = (spec.get("description") or "").strip()
            lines.append(f":param {prop}: {desc}".rstrip())
    return "\n".join(lines)


def _identifier(name: str) -> str:
    """Coerce an MCP name into a valid Python identifier (names may use '-')."""
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    return cleaned if cleaned.isidentifier() else f"_{cleaned}"
