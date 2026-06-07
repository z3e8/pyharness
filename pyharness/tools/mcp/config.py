"""Declare MCP servers in a config file instead of in code.

The config uses the de-facto-standard `mcpServers` shape (the same one Claude
Desktop and `.mcp.json` use), so a server is local or remote by which key it
carries:

    {
      "mcpServers": {
        "weather": {                       # local: run a subprocess
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-weather"],
          "env": {"WEATHER_API_KEY": "secret:weather_key"}
        },
        "github": {                        # remote: Streamable HTTP endpoint
          "url": "https://mcp.example.com/mcp",
          "headers": {"Authorization": "Bearer secret:github_token"}
        }
      }
    }

`secret:NAME` values in `env`/`headers` are resolved through the session `Vault`
in the parent — so no cleartext credential lives in the config file (design §5).
"""

from __future__ import annotations

import json
from pathlib import Path

_SECRET_PREFIX = "secret:"


def load_config(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def mount_config(registry, config: dict | str | Path, *, vault=None, lazy: bool = True) -> list[str]:
    """Mount every server declared in `config` into `registry`. `config` may be a
    dict or a path to a JSON file. Returns the registered tool names.

    Lazy by default: servers are *registered* but not contacted until the agent
    first searches or uses them, so a slow or down server can neither delay nor
    abort session startup (it surfaces as unavailable only when reached). Pass
    `lazy=False` to connect eagerly and fail fast. `secret:NAME` credentials are
    resolved through the vault now, in the parent, in either mode."""
    if isinstance(config, (str, Path)):
        config = load_config(config)
    servers = config.get("mcpServers", config)
    names = []
    for name, spec in servers.items():
        env = _resolve_secrets(spec.get("env"), vault)
        headers = _resolve_secrets(spec.get("headers"), vault)
        if lazy:
            names.append(
                registry.register_lazy(
                    name,
                    _loader(name, spec, env, headers),
                    source="installed",
                    summary=spec.get("summary") or f"MCP server {name!r} (not yet connected)",
                )
            )
        else:
            names.append(
                registry.add_mcp_server(
                    name,
                    spec.get("command"),
                    tuple(spec.get("args", [])),
                    url=spec.get("url"),
                    env=env,
                    headers=headers,
                    cwd=spec.get("cwd"),
                    summary=spec.get("summary"),
                )
            )
    return names


def _loader(name: str, spec: dict, env: dict | None, headers: dict | None):
    """Build the deferred connect-and-wrap thunk for one lazily-mounted server."""
    from .client import wrap_mcp_server

    def load():
        return wrap_mcp_server(
            name,
            spec.get("command"),
            tuple(spec.get("args", [])),
            url=spec.get("url"),
            env=env,
            headers=headers,
            cwd=spec.get("cwd"),
            summary=spec.get("summary"),
            timeout=spec.get("timeout", 30.0),
        )

    return load


def _resolve_secrets(mapping: dict | None, vault) -> dict | None:
    """Replace `secret:NAME` values with the vault's cleartext (parent-side)."""
    if not mapping:
        return None
    resolved = {}
    for key, value in mapping.items():
        if isinstance(value, str) and value.startswith(_SECRET_PREFIX):
            if vault is None:
                raise ValueError(f"{key!r} references a secret but no vault was provided")
            value = vault.get(value[len(_SECRET_PREFIX):])
        resolved[key] = value
    return resolved
