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

`secret:NAME` values in `env`/`headers` are resolved through a `SecretSink` in
the parent — so no cleartext credential lives in the config file (design §5),
and the sink learns every value it hands out so the session can mask it back out
of anything the agent later reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

_SECRET_PREFIX = "secret:"


def load_config(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def mount_config(
    registry,
    config: dict | str | Path,
    *,
    sink=None,
    lazy: bool = True,
    allowed_hosts: frozenset[str] | None = None,
) -> list[str]:
    """Mount every server declared in `config` into `registry`. `config` may be a
    dict or a path to a JSON file. Returns the registered tool names.

    Lazy by default: servers are *registered* but not contacted until the agent
    first searches or uses them, so a slow or down server can neither delay nor
    abort session startup (it surfaces as unavailable only when reached). Pass
    `lazy=False` to connect eagerly and fail fast. `secret:NAME` credentials are
    resolved through `sink` now, in the parent, in either mode — a `SecretSink`,
    not a bare `Vault`, so every value handed to a server is one the session can
    mask back out of what the agent reads (an MCP server that echoes its own
    credential in a tool result is a real shape, and the harness cannot stop it
    echoing — only stop the echo reaching agent code).
    `allowed_hosts` is the mounting session's host scope, enforced on every
    remote (HTTP) server's URL at connect and per request."""
    if isinstance(config, (str, Path)):
        config = load_config(config)
    servers = config.get("mcpServers", config)
    names = []
    for name, spec in servers.items():
        # Where this server's credentials are about to travel, for the sink's
        # host-binding check. A remote server has one: its URL's host. A local
        # (stdio) server has none — the credential goes into a subprocess env,
        # which is not a host at all — so a host-bound secret is refused there
        # rather than silently released into a destination the binding cannot
        # describe. Unbound secrets are unaffected in both cases.
        target_host = urlsplit(spec["url"]).hostname if spec.get("url") else None
        env = _resolve_secrets(spec.get("env"), sink, target_host)
        headers = _resolve_secrets(spec.get("headers"), sink, target_host)
        # Discovery metadata declared alongside the server, forwarded to the
        # registry so a lazily-mounted server is findable by more than its name.
        meta = dict(
            keywords=tuple(spec.get("keywords", ())),
            category=spec.get("category"),
            featured=bool(spec.get("featured", False)),
        )
        if lazy:
            names.append(
                registry.register_lazy(
                    name,
                    _loader(name, spec, env, headers, allowed_hosts),
                    source="installed",
                    kind="mcp",
                    summary=spec.get("summary")
                    or f"MCP server {name!r} (not yet connected)",
                    **meta,
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
                    timeout=spec.get("timeout", 30.0),
                    allowed_hosts=allowed_hosts,
                    **meta,
                )
            )
    return names


def _loader(
    name: str,
    spec: dict,
    env: dict | None,
    headers: dict | None,
    allowed_hosts: frozenset[str] | None = None,
):
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
            allowed_hosts=allowed_hosts,
        )

    return load


def _resolve_secrets(
    mapping: dict | None, sink, target_host: str | None = None
) -> dict | None:
    """Replace `secret:NAME` values with cleartext (parent-side), through the
    sink rather than the vault directly: the sink enforces the secret's host
    binding against `target_host` and records the cleartext's mask forms, so a
    value this server later echoes back is masked out of what the agent reads."""
    if not mapping:
        return None
    resolved = {}
    for key, value in mapping.items():
        if isinstance(value, str) and value.startswith(_SECRET_PREFIX):
            if sink is None:
                raise ValueError(
                    f"{key!r} references a secret but no secret sink was provided"
                )
            value = sink.resolve(value[len(_SECRET_PREFIX) :], target_host=target_host)
        resolved[key] = value
    return resolved
