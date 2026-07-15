from __future__ import annotations

import functools
import inspect
import json
from pathlib import Path
from types import ModuleType

from ...security.grants import GrantScope
from ...security.policy import ActionCategory
from ...tools.registry import Registry, _public_functions
from ...util import summarize_args

_SECRET_PREFIX = "secret:"


def mcp_tool_meta(registry: Registry, tool, func) -> tuple[bool, dict | None]:
    """The MCP descriptor behind one `tools.invoke` target, **without ever
    connecting a server** (policy runs before execution; deciding it must not
    spawn the server as a side effect). Returns `(is_mcp, meta)`: `(False,
    None)` for a non-MCP target, `(True, {"name", "annotations"})` for a
    resolved MCP function, and `(True, None)` when the MCP entry is not yet
    resolved or the function is unknown — callers fail closed on that."""
    info = registry.info(str(tool)) if tool is not None else None
    if info is None or info.kind != "mcp":
        return False, None
    if info.module is None:
        return True, None
    return True, getattr(info.module, "_mcp_tools", {}).get(str(func))


def unvetted_mcp_call(registry_getter):
    """Build the `approve_if` predicate that forces approval on a `tools.invoke`
    targeting an MCP server tool, unless the server declares it read-only
    (`readOnlyHint`). `registry_getter` is called at decision time (the default
    Policy is built before the Session's registry exists). Never connects a
    server — an unresolved MCP target fails closed. Non-MCP targets (installed
    modules, learned skills) stay ungated here."""

    def predicate(action: str, args: tuple, kwargs: dict) -> bool:
        if action != "tools.invoke":
            return False
        tool, func, _, _ = _invoke_target(args, kwargs)
        is_mcp, meta = mcp_tool_meta(registry_getter(), tool, func)
        if not is_mcp:
            return False
        return meta is None or not meta["annotations"].get("readOnlyHint", False)

    return predicate


def _invoke_target(args: tuple, kwargs: dict) -> tuple:
    """The (tool, func, call_args, call_kwargs) of an invoke call, from either
    calling convention (child IPC sends positionals; JSON turns tuples into
    lists)."""
    tool = kwargs.get("tool", args[0] if len(args) >= 1 else None)
    func = kwargs.get("func", args[1] if len(args) >= 2 else None)
    call_args = kwargs.get("args", args[2] if len(args) >= 3 else ())
    call_kwargs = kwargs.get("kwargs", args[3] if len(args) >= 4 else {})
    return tool, func, tuple(call_args or ()), dict(call_kwargs or {})


class ToolsCapability:
    name = "tools"

    def __init__(self, registry: Registry, *, broker=None, vault=None, mcp_config_path=None):
        self.registry = registry
        self._broker = broker  # set by Session; None leaves use_tool ungated
        self._vault = vault  # resolves secret:NAME refs when mounting a server
        self._mcp_config_path = Path(mcp_config_path) if mcp_config_path else None

    def exports(self) -> dict:
        return {
            "search_tools": self.search_tools,
            "describe_tool": self.describe_tool,
            "use_tool": self.use_tool,
            "invoke": self.invoke,
            "add_mcp_server": self.add_mcp_server,
        }

    def search_tools(self, query: str = "", include_all: bool = False) -> str:
        return self.registry.search(query, include_all=include_all)

    def describe_tool(self, name: str) -> str:
        return self.registry.describe(name)

    def use_tool(self, name: str) -> ModuleType:
        """Load a tool and return it as a module of broker-gated functions.

        A module that is already broker-gated (a core capability surfaced via
        `as_tool_module`) passes through untouched — wrapping it again would
        gate the same call twice. Everything else (MCP wrappers, installed
        modules, learned skills) is returned as proxies routing through
        `tools.invoke`, so in-process calls get the same policy/audit/approval
        treatment the out-of-process child already gets via `RemoteToolSpec`."""
        module = self.registry.use(name)
        if self._broker is None or getattr(module, "_broker_gated", False):
            return module
        gated = ModuleType(name)
        gated.__doc__ = module.__doc__
        for fname, func in _public_functions(module):
            proxy = self._gated_proxy(name, fname)
            functools.wraps(func)(proxy)  # copy __doc__/__wrapped__ (signature)
            proxy.__module__ = name  # after wraps, so _public_functions still finds it
            setattr(gated, fname, proxy)
        return gated

    def _gated_proxy(self, tool: str, fname: str):
        def proxy(*args, **kwargs):
            return self._broker.call("tools", "invoke", tool, fname, args, kwargs)

        proxy.__name__ = fname
        return proxy

    def invoke(self, tool: str, func: str, args: tuple, kwargs: dict):
        """Call a tool's function through the broker. Both kernel modes route
        tool-module calls here — the child via its `RemoteToolSpec` proxy, the
        in-process kernel via `use_tool`'s gated proxies — so every call gets
        the same policy/audit/budget gating as any other capability."""
        return getattr(self.registry.use(tool), func)(*args, **kwargs)

    def add_mcp_server(
        self,
        name: str,
        command: str | None = None,
        args: tuple = (),
        *,
        url: str | None = None,
        env: dict | None = None,
        headers: dict | None = None,
        summary: str | None = None,
        keywords: tuple = (),
        category: str | None = None,
        timeout: float = 30.0,
        save: bool = False,
    ) -> str:
        """Mount an MCP server for this session — local (`command` + `args`) or
        remote (`url`) — as one tool module named `name`, findable via
        search_tools and loadable via use_tool. Mounting is lazy: the server is
        contacted on first describe/use, not now. Requires human approval
        (mounting a server installs code).

        Credential values in `env`/`headers` should be vault references
        (`"secret:NAME"`), resolved parent-side and never visible to you.
        `save=True` also persists the server to the session's MCP config file so
        later sessions mount it automatically — that path *requires* `secret:`
        refs for every env/header value; a cleartext credential is refused."""
        if self.registry.info(name) is not None:
            raise ValueError(
                f"a tool named {name!r} already exists; pick a different server name"
            )
        if bool(command) == bool(url):
            raise ValueError("specify exactly one of command (local) or url (remote)")
        spec = {
            key: value
            for key, value in {
                "command": command,
                "args": list(args),
                "url": url,
                "env": dict(env) if env else None,
                "headers": dict(headers) if headers else None,
                "summary": summary,
                "keywords": list(keywords),
                "category": category,
                "timeout": timeout,
            }.items()
            if value
        }
        if save:
            self._save_server(name, spec)
        from ...tools.mcp import mount_config

        mount_config(self.registry, {"mcpServers": {name: spec}}, vault=self._vault)
        saved = f" and saved to {self._mcp_config_path}" if save else ""
        return f"mounted MCP server {name!r} (lazy; connects on first use){saved}"

    def _save_server(self, name: str, spec: dict) -> None:
        """Merge one server spec into the session's MCP config file, refusing to
        persist anything that isn't a `secret:` reference in env/headers — the
        config file's contract is that no cleartext credential lives in it."""
        if self._mcp_config_path is None:
            raise ValueError(
                "this session has no MCP config path to save to; "
                "mount with save=False, or configure PYHARNESS_MCP_CONFIG"
            )
        for field in ("env", "headers"):
            for key, value in (spec.get(field) or {}).items():
                if not (isinstance(value, str) and value.startswith(_SECRET_PREFIX)):
                    raise ValueError(
                        f"{field}[{key!r}] is not a 'secret:NAME' reference; refusing to "
                        "write a possible cleartext credential to the config file. Store "
                        "the value in the vault and pass 'secret:NAME' instead."
                    )
        path = self._mcp_config_path
        config = json.loads(path.read_text()) if path.exists() else {}
        config.setdefault("mcpServers", {})[name] = spec
        path.write_text(json.dumps(config, indent=2) + "\n")

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """Describe a gated call for the approver. For `invoke` on an MCP tool,
        the category comes from the server's declared annotations: an explicit
        `destructiveHint` is IRREVERSIBLE (always re-asks, never grantable),
        anything else is OUTWARD. Deliberate deviation from the MCP spec's
        default (absent destructiveHint *means* destructive): taking that
        literally would class most tools IRREVERSIBLE and remove the one-grant-
        per-server flow; the prompt itself is the safety net for the unknown."""
        if op == "invoke":
            tool, func, call_args, call_kwargs = _invoke_target(args, kwargs)
            summary = f"{tool}.{func}({summarize_args(call_args, call_kwargs)})"
            is_mcp, meta = mcp_tool_meta(self.registry, tool, func)
            if is_mcp and meta and meta["annotations"].get("destructiveHint"):
                return ActionCategory.IRREVERSIBLE, summary
            return ActionCategory.OUTWARD, summary
        if op == "add_mcp_server":
            # Secret-safe: env/header *names* only, never values (which may be
            # credentials the agent was allowed to pass as secret: refs).
            bound = inspect.signature(self.add_mcp_server).bind(*args, **kwargs)
            bound.apply_defaults()
            b = bound.arguments
            target = f"command: {b['command']}" if b.get("command") else f"url: {b['url']}"
            parts = [target]
            if b.get("env"):
                parts.append(f"env: {', '.join(b['env'])}")
            if b.get("headers"):
                parts.append(f"headers: {', '.join(b['headers'])}")
            if b.get("save"):
                parts.append("save: persists to the MCP config file")
            summary = f"mount MCP server {b['name']!r} ({'; '.join(parts)})"
            return ActionCategory.OUTWARD, summary
        return ActionCategory.OUTWARD, f"tools.{op}({summarize_args(args, kwargs)})"

    def scope(self, op: str, args: tuple, kwargs: dict) -> GrantScope | None:
        """The grant key for a gated invoke: action-class "mcp" plus the server
        name, so one approval can cover a server's (non-destructive) tools for
        the session. Destructive and not-yet-resolved targets yield None (not
        grantable — always prompt)."""
        if op != "invoke":
            return None
        tool, func, _, _ = _invoke_target(args, kwargs)
        is_mcp, meta = mcp_tool_meta(self.registry, tool, func)
        if not is_mcp or meta is None or meta["annotations"].get("destructiveHint"):
            return None
        return GrantScope("mcp", str(tool))
